"""The turn: observe, decide, act, record.

Everything below this composes; nothing below this composes anything. `reconcile` decides, `observe_*` reads, `Fleet.spawn` acts, the journal records — and until now nothing called them together. This is that call, and it is deliberately the only place in the package that knows about all four at once, which is why it lives above them rather than inside `_schedule` (whose purity is load-bearing, and which `_workspace` already imports).

**Two dispositions of one function.** `tend` executes the actions; `status` computes them and throws them away.

| verb | actions | claim | writes |
|---|---|---|---|
| `status` | computed, **discarded** | reported, never taken | nothing at all |
| `tend` | computed, **executed** | held for the seconds it runs | journal, `status.md`, the log directory |

They cannot drift, because they are the same code path with one flag. That is worth more than the duplication it saves: a preview that disagrees with what the next turn actually does is worse than no preview.

**`status` must stay read-only, because every convention in the ecosystem promises it is.** `git status`, `systemctl status`, `docker ps` — somebody typing `steward status` to satisfy their curiosity about an overnight sweep must not thereby launch eight workers. Surprise as a side effect of looking is the one thing a runner of expensive jobs cannot afford. It does not even write `steward.log`, and the cost of that (a failed status leaves no trace) is smaller than the cost of a read verb that writes.

**A turn never blocks.** Everything with an unbounded duration is a detached child that some later turn observes finishing. That is what keeps the claim short-lived, which is in turn what makes a claim older than a generous threshold unambiguously stale — the property the whole driver model rests on (execution.md, *A scan is a detached process, not part of a tend*).

**An interrupted turn is reconciled by the next one**, and that is a requirement rather than a hope. There is no resume path here and no partial-turn state to recover: the next turn re-reads the log directory and the process table and decides again from what it finds. Every write is ordered so that being interrupted before it costs a repeat rather than a corruption.
"""

import hashlib
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from .._anomaly.applied import read_applied
from .._anomaly.fold import Pending, absorb, as_events, read_anomalies
from .._anomaly.model import Anomalies
from .._evalset.archive import archive_log
from .._evalset.cache import read_attempt_cache, write_attempt_cache
from .._evalset.instances import (
    read_classed_cache,
    sample_uuids,
    write_classed_cache,
)
from .._evalset.manifest import (
    Manifest,
    ManifestScan,
    definition_hash,
    manifest_digest,
    read_manifest,
    worker_overrides,
)
from .._evalset.observe import (
    ObservedLogs,
    ObservedTasks,
    TaskState,
    UnreadableLog,
    observe_logs,
    observe_tasks,
)
from .._notify import Channel, describe_channel, establish_channel
from .._scan import (
    ScanFindings,
    establish_scan_model,
    existing_eval_set_id,
    merged_scanners,
    scan_dir_location,
    scan_findings,
    sync_scan,
)
from .._schedule import (
    Action,
    ArchiveLog,
    InFlight,
    Pool,
    ReapWorker,
    SpawnTask,
    SpawnWorker,
    Summary,
    reconcile,
    resolve_samples_ramp,
)
from .._util.duration import is_after, seconds_since
from .._worker import (
    DEFAULT_STUCK_AFTER,
    Fleet,
    LiveFleet,
    LiveTarget,
    Unavailable,
    read_fleet,
    record_exited,
    resolve_eval_set_id,
    resolve_inflight,
    task_config,
)
from .._workspace import (
    ACTION,
    ARMED,
    OBSERVATION,
    Ack,
    Armed,
    Claim,
    Collected,
    DamagedLine,
    Directives,
    DirectivesError,
    Held,
    JournalEvent,
    Paused,
    Raised,
    RampHold,
    Signature,
    Workspace,
    acquire,
    append_event,
    declared_notification,
    declared_scan_model,
    read_acks,
    read_armed,
    read_claim,
    read_collected,
    read_directives,
    read_journal,
    read_launched,
    read_overrides,
    read_pause,
    read_raised,
    read_ramp_holds,
    read_signoff,
    read_undelivered,
    resolve_log_dir,
    resolve_log_store,
    resolve_pool,
    steward_log,
    sync_target,
    sync_workspace,
    truncate_log,
    utc_now,
)
from .analysis_md import Section, analysis_sections, merge_analysis
from .anomalies_md import Caveat, anomalies_markdown, caveats
from .coverage import Coverage, coverage
from .detect import detect, scan_attempts, task_health
from .history import Happened, happened
from .items import (
    Item,
    Supervision,
    Verdict,
    signed_off,
    tend_items,
    unfinished,
    verdict,
)
from .notify import held_tasks, notify_turn
from .progress import Progress, live_totals, task_progress
from .render import marks_note, status_markdown
from .rulings import (
    Dispositions,
    accepted_tasks,
    affected_refs,
    apply_rulings,
    dispositions,
    policy_rulings,
    rerun_ruled,
)
from .tuning import (
    Baseline,
    Move,
    TaskSignals,
    TuningPlan,
    observation_payload,
    plan_tuning,
    read_baseline,
    read_ramp_record,
    signals,
)

SCAN_FOLD_FAILED = "scan_fold_failed"
SCAN_FOLD_RESTORED = "scan_fold_restored"
"""The scan fold's episode edges, on `status_unwritable`'s pattern and folded beside it (`_episodes`).

Bare strings on the `sync_failed` / `status_unwritable` model: an episode is *when this started*, so the pair is a switch, and what it buys is the retry. Without it the fold's cheap `running or departed` gate has a hole with a bad ending — a failure on the departure turn is a fold that never happens, because the reap takes the gate away and the next thing to fold is signoff's terminal finalize, which runs after the gate has already passed.
"""


class TendError(Exception):
    """A turn could not be run at all.

    Distinct from a turn that ran and found problems, which is a `TendResult` with things in it. This is the machinery failing rather than the run.
    """


@dataclass(frozen=True)
class Refused:
    """A tend that did not run, because somebody else holds the claim.

    A value rather than an error, and the caller should treat it as an ordinary outcome: a timer firing while an agent is mid-tend is the common case, not the edge, and the right response is to do nothing at all. The work is already being done.
    """

    held: Held


@dataclass(frozen=True)
class TendResult:
    """What a turn saw, and what it did about it."""

    summary: Summary
    """Where the run stands."""

    queued: list[SpawnTask]
    """Would have been started, but the run's shape has no room for them yet."""

    drift: bool
    """The definition file no longer hashes to what the committed manifest recorded.

    Reported and never acted on. Capture is expensive and a definition is a file a human edits live, so a turn that re-read it automatically would eventually read a half-saved edit — and applying a delta means weighing whether it looks like a mistake, which is judgement (workflow.md, *One trigger, and one gate on it*).
    """

    degraded: str | None
    """Why `_steward.yaml` could not be read, when a turn ran on the last known good settings anyway."""

    claim: Held | None
    """Who holds the run claim. Only ever set by `status`, which reports one rather than taking one."""

    broke: Held | None
    """The wedged holder this turn killed to get the claim, if there was one."""

    definition_hash: str | None = None
    """What the definition hashes to now. Carried because it is what keys the drift item: a further edit makes a new id, so an accepted drift stays accepted and the next one is heard."""

    manifest_digest: str | None = None
    """A digest of the task set the committed manifest asks for.

    **What identifies a set of *results*, and deliberately not `definition_hash`**, which identifies a *file*. The two come apart in both directions: an edit sitting unlaunched changes the file and not the results, and a Flow spec relaunched with different arguments — or one whose imported module changed — produces different tasks from a byte-identical file. Anything keyed on the file hash therefore both re-opens settled decisions and silently suppresses new ones. See `manifest_digest`.
    """

    degraded_at: str | None = None
    """`_steward.yaml`'s modification time when it would not parse, for the same reason — an edited file that still fails is a new item rather than one already acknowledged."""

    supervision: Supervision | None = None
    """Whether anything is scheduled to run the next turn. `None` where nothing asked — an item projection over a result assembled by hand has no opinion about timers."""

    items: list[Item] = field(default_factory=list[Item])
    """Everything this turn has to say that is not a number, acknowledged ones already removed."""

    verdict: Verdict = Verdict.CLEAR
    """Where the run stands. Computed over the items and the run, not over the worst thing in it."""

    appeared: list[str] = field(default_factory=list[str])
    """Item ids not in the previous turn's. What an edge notification fires on (step 24)."""

    resolved: list[str] = field(default_factory=list[str])
    """Item ids the previous turn had and this one does not — closed, acknowledged, or simply over."""

    finished: list[str] = field(default_factory=list[str])
    """Identifiers of tasks complete now that the previous turn did not have complete.

    The third diff, beside `appeared` and `resolved`, and the one that is not about items: it is what a `progress` notification names. Batched by the turn rather than fired per task, because the tend is already the clock — a sweep that finishes five tasks in one interval is one message naming five (`_notify.post.Kind.PROGRESS`).

    Empty on the first turn that records a completion set at all, since there is nothing to diff against and *everything already finished* is not news.

    Identifiers rather than the keys a reader recognises, for the reason `_finished` gives: this is diffed against a record an earlier turn wrote, and a display key is computed against whatever else was on screen at the time.

    Held tasks are subtracted (`held`), which is what makes a hold a deferral: the diff is against a set they are also missing from, so they re-enter it every turn until released.
    """

    held: frozenset[str] = frozenset()
    """Identifiers whose completion is waiting on an agent's scan investigation (`_notify.held_tasks`).

    Subtracted from `finished` **and** from the completion set the observation records, and both are load-bearing. Subtracting from only the first would spend the diff: the next turn reads the task as already-recorded-complete, and the finish is announced never.
    """

    spawned: list[str] = field(default_factory=list[str])
    reaped: list[str] = field(default_factory=list[str])
    archived: list[str] = field(default_factory=list[str])

    failures: list[str] = field(default_factory=list[str])
    """Actions that could not be carried out. One failing action does not fail the turn — the rest still run, and the next turn decides again from what it finds."""

    executed: bool = False
    """Whether the actions were carried out (`tend`) or discarded (`status`)."""

    happened: Happened = field(default_factory=Happened)
    """What has been done to this run, oldest first — the summary's third section.

    Computed here rather than in the renderer because it is a fold over the journal this turn already read, and because two renderings of it would be two chances to disagree.
    """

    collected: Collected | None = None
    """The most recent collection, or `None` where no agent has attached.

    Two things read it: the delta `steward collect` shows, and the collection age beside the tend age — the pair that separates *the timer stopped* from *nobody is looking* (agent.md §2.2).
    """

    since_collected: float | None = None
    """Seconds since an agent last collected, or `None` where none ever has.

    Beside `Supervision.since_tend` rather than inside it: that type is about whether a *timer* is firing, and this is about whether anyone is reading what it produces. Two failures, two ages (agent.md §2.2).
    """

    position: int = 0
    """The journal's last line at the moment this turn read it. What a collection advances the cursor *to* — taken from the read rather than from the file's length now, so a `collect` acknowledges what it was shown rather than whatever landed while it was being shown."""

    progress: Progress = field(default_factory=Progress)
    """One row per task: samples done, in flight, and queued, the budget the leading sample is closest to spending, and the headline metric.

    Where `summary` counts what Steward is doing, this is what the *run* is doing — the question anybody opening a status view actually has. Sample counts and the live columns come from different places (the log header and the worker's own socket), which is why it is assembled here rather than inside `reconcile`.
    """

    policies: list[str] = field(default_factory=list[str])
    """Standing rules in force, from `_steward.yaml` or `STEWARD_POLICIES`. Reported so that an agent reading the file alone is not missing half of them."""

    log_store: str | None = None
    """The reuse store this workspace configures, or `None`. Reported for one reason: `signoff --publish` is the only act at the end of a run that nothing does by default, so the readiness item has to say a decision is waiting."""

    tuning: TuningPlan = field(default_factory=TuningPlan)
    """What this turn's window supports retuning, and the account of why.

    Computed for both dispositions and executed by one, exactly like the actions: a `status` shows the step a clean window has earned without taking it, which is the preview contract everything else here honours.
    """

    log_dir: str | None = None
    """Where this run's results are, as the launch that committed the manifest resolved it.

    **Reported because it stopped being guessable.** For as long as `logs/` was the near-universal answer, a reader who needed the directory could assume it; a definition naming its own was the exception. A `log_root` makes the workspace's `logs/` the exception instead — there is no such directory at all — while the runbook goes on telling an agent to reach for `samples_df` and `read_eval_log_sample_summaries` without saying where. The alternative was pointing the agent at `.steward/manifest.json`, which would make a private file part of the contract with the one directory Steward documents as safe to delete.

    `None` only on a result assembled by hand, which makes no claim about a directory.
    """

    notification: Channel | None = None
    """Where this run's notifications go, named without its value (`_notify.Channel`).

    **Reported for the reason `log_dir` is**: it stopped being guessable, and the ways of guessing are all wrong. A channel arrives from four spellings, and the one that most often carries it is a `.env` at or above the workspace — loaded into Steward's own process by `init_dotenv()` and into no shell an agent can read. So an agent that opened `_steward.yaml`, found the key commented out, and told a person the run could reach nobody was right about everything it looked at and wrong about the run.

    A fact rather than an item, on the argument `_cli.launch._echo_no_channel` makes: the remedy is said once, at launch, where somebody is watching. `None` only on a result assembled by hand, which settled no channel.
    """

    scan: ManifestScan | None = None
    """What this run scans with, as the committed manifest holds it.

    Carried so signoff can close the scan bracket without re-reading the manifest for the one field it needs — the `scans` redirect, which is the only thing that says where the rows actually are. `None` for a run committed before scanning existed, or a result assembled by hand.
    """

    scan_id: str | None = None
    """This run's scan id — its eval set id, as the log directory or the manifest says it.

    Resolved by **reading** rather than by `resolve_eval_set_id`, which mints and writes one: a `status` must leave the log directory exactly as it found it. Carried so the turn's fold and signoff's finalize cannot come to disagree about which directory they are acting on.
    """

    unwritten: dict[str, str] = field(default_factory=dict[str, str])
    """Task identifier to display key, for every `analysis.md` section that carries facts and no reading of them.

    Computed against the file **as this turn's merge leaves it**, which is what makes a `status` honest: a task whose section does not exist yet is a task with nothing written, whether or not the section has been appended.
    """

    coverage: Coverage = field(default_factory=Coverage)
    """How much of what landed the scanners actually reached, per task and run-wide (`coverage.Coverage`).

    Empty for a run that scans nothing, which is what keeps the column and the note off a document that has no scanning to report.
    """

    journal_damage: list[DamagedLine] = field(default_factory=list[DamagedLine])
    """Journal lines this turn's history read could not turn into events.

    Every fold the turn ran — the pause, the acks, the diff baseline — ran without whatever these lines said, so the damage is a caveat on this turn's own answers and not merely a fact about a file. It becomes the agent's item (`items.JOURNAL_DAMAGE`); the honest repair is reading the lines and re-journalling what they meant, which is judgement.
    """

    status_failing: str | None = None
    """When `status.md` stopped being writable, or `None` while it writes.

    From the journal's episode record rather than from this turn's own attempt, which happens after the items are computed — so a fresh failure surfaces on the next turn, and a restored one clears the same way. The episode's opening instant, which is what keys the item.
    """

    fold_failing: str | None = None
    """When the scan fold started failing, or `None` while it folds.

    Unlike the two beside it this raises no item: what it costs is freshness, and the read it feeds goes on answering from the rows already compacted. What it must not do is go unnoticed at the one moment it changes an answer, so signoff warns on it — a signature taken while rows are still unfolded is a signature over results this run has not finished looking at.
    """

    sync_failing: dict[str, str] = field(default_factory=dict[str, str])
    """Destinations the workspace has stopped propagating to, each with when it stopped. The same episode mechanics as `status_failing`, per target."""

    breaks: int = 0
    """Consecutive turns that each had to break a wedged claim, this one included.

    One is recovery working as designed. Two or more is a tend that wedges deterministically — killed and reincarnated every interval, each incarnation destroying the evidence of the last — which is the kill loop `items.KILL_LOOP` names (execution.md §9).
    """

    breaks_since: str | None = None
    """When the current run of breaks began, or `None` where there is none. What keys the item, so a later, separate loop is a new question."""

    anomalies: Anomalies = field(default_factory=Anomalies)
    """Every anomaly window, open and settled, with this turn's census already absorbed.

    For a tend, the state the journal now holds; for a `status`, the state it *would* hold — the same fold over the same pending events, which is what makes the preview honest. What the items, the signoff gate, and the anomalies section all read.
    """

    anomaly_pending: list[Pending] = field(default_factory=list[Pending])
    """The window events this turn's census implies. A tend has appended them by the time it returns; a `status` leaves them unwritten — which is why a deciding verb persists its targets' share (`_cli.anomalies.persist_windows`) before its decision, so a ruling never lands against a window the journal does not hold."""

    dispositions: Dispositions = field(default_factory=Dispositions)
    """Per task, what each errored sample's class has been ruled — the errored cell's split and the "Scores are over n of m" note (`_tend.rulings.dispositions`)."""

    stuck_cancel: bool | tuple[str, ...] | None = None
    """Which stuck pending tool calls the agent may cancel, normalized — `True` for any, a tuple of function names, `None` for none. What routes a `stuck` item's owner."""

    observed: ObservedTasks | None = None
    """The manifest read against the log directory, exactly as this turn read it.

    Carried for `signoff`, which curates the superseded attempts out of `logs/` and must do it against the same observation its gate judged. A second `observe_logs` there would be a second set of numbers — one deciding the run is settled and another deciding which logs it settled on — and the two can disagree about a file a worker landed in between. `None` on a result assembled by hand, which read no directory.
    """

    acknowledged: dict[str, Ack] = field(default_factory=dict[str, "Ack"])
    """What has been disposed of, by item id.

    Carried because an acknowledgment is the **second way into `anomalies.md`** — one whose subject left a mark on the results is a caveat exactly as a ruling is (workflow.md §14) — and `status.md` renders the same caveats one line each. Both were reading the fold without it and quietly dropping every acked caveat, which is *removed from the surface* silently meaning *removed from the record*.
    """

    current_logs: dict[str, str] = field(default_factory=dict[str, str])
    """Task identifier to its current attempt's log location.

    The narrowing every report-facing count needs: a sample that failed, was re-run and failed again is two instances of one row, and only the current attempt is in the results (`rulings.affected_refs`).
    """

    rendered: list[str] = field(default_factory=list[str])
    """The generated documents this turn actually wrote, by name.

    Empty on a `status`, which writes neither by design. **Reported rather than inferred**, because the two ways of guessing are both wrong: a turn that returns proves nothing (`_write_rendered` swallows an `OSError` so a failed write never fails a turn that already happened), and a file's existence proves less — a stale document from an earlier turn exists. `signoff` is the one caller that needs the answer, since it disarms the timer straight afterwards and no later turn will repair what this one could not write.
    """

    caveats: list[Caveat] = field(default_factory=list["Caveat"])
    """What reached the final data, decided once (`_tend.anomalies_md.caveats`).

    **The one place three readers agree**: `anomalies.md`'s five-field entries, `status.md`'s one-line marks, and the exceptions a signature names. Deciding what a caveat is needs the census — which of a window's instances are in the *current* attempt — and the census is the largest thing a turn holds and has no business on a value `status --json` prints. So the answer travels rather than the evidence, and a decision this list drops cannot survive as a line under the status heading.
    """

    signature: Signature | None = None
    """The most recent attestation, or `None` where nobody has signed.

    The raw record rather than the verdict over it, because the two readers want different halves: the projections ask *does it still stand* (`signed`), and the gate refusing a second signature has to name who signed and when.
    """

    launched: str | None = None
    """When this run was most recently launched, or `None` where nothing ever launched it.

    Read by `signed`: a relaunch releases every acceptance latch, so it must also un-sign — and an unchanged manifest relaunched has the same digest, which is the case the digest test alone cannot see.
    """

    @property
    def signed(self) -> bool:
        """Whether an attestation is in force over this run.

        A property rather than a field so that the three surfaces asking it — the verdict, the readiness item, and the signoff gate — cannot answer differently, and so that a result assembled by hand cannot claim to be signed without a signature (`items.signed_off`).
        """
        return signed_off(
            self.signature,
            digest=self.manifest_digest,
            anomalies=self.anomalies,
            launched=self.launched,
        )


def tend(
    workspace: Workspace,
    *,
    max_workers: int | None = None,
    stall_after: int | None = None,
    stuck_after: int | None = None,
    preauthorized: dict[str, str] | bool | None = None,
    samples_ramp: tuple[int, int] | bool | None = None,
    sync: str | bool | None = None,
    notification: str | bool | None = None,
    scan_model: str | bool | None = None,
    break_stale: bool = True,
    claim: Claim | None = None,
) -> TendResult | Refused:
    """Run one turn of the supervision loop.

    Args:
        workspace: The workspace to tend.
        max_workers: Worker processes for this turn, overriding `_steward.yaml`. `None` expresses no preference and defers to the file, which itself defaults to a process per task — it does not request that width, so a workspace that sets the key cannot be widened back to unbounded for one turn.
        stall_after: Fruitless respawns before a task is given up on, overriding `_steward.yaml`.
        stuck_after: Seconds of sample silence before a `stuck` item, overriding `_steward.yaml`.
        preauthorized: Class patterns to dispositions, overriding `_steward.yaml` — the rulings granted in advance this turn may apply. `False` declines every standing grant for this turn; `None` defers to the file.
        samples_ramp: The ramp's envelope for this turn, overriding `_steward.yaml`. A narrower range brings running tasks back inside it.
        sync: Where to propagate the workspace this turn, overriding `_steward.yaml`. `False` propagates nowhere; `None` defers to the file, which itself defaults to the log directory.
        notification: Where Steward posts this turn, overriding `_steward.yaml`. `False` silences Steward and never the fleet; `None` defers to the file, then to `INSPECT_EVAL_NOTIFICATION`. Settled before anything spawns, because it is also the channel every worker this turn starts will inherit (`_notify.channel`).
        scan_model: The model scanners use this turn, overriding `_steward.yaml`. `False` configures none — scanners fall to each sample's own model; `None` defers to the file, then to `SCOUT_SCAN_MODEL`. Settled before anything spawns, the way `notification` is and for the same reason (`_scan.model`).
        break_stale: Kill a wedged claim holder and take the claim from it.
        claim: A claim the caller already holds, to run this turn under instead of taking one. For `launch`, whose whole composition — capture, commit, arm, tend — is one span of single-writer work: a launch that released before its own first turn would be refused by it, or worse, would let a timer firing in the gap spawn workers for tasks the commit had just orphaned. Released by the caller, not here, because the caller's work is not over.

    Returns:
        What the turn saw and did, or a `Refused` naming the holder that would not give up the claim. Never `Refused` when `claim` is given — the claim is already in hand.

    Raises:
        TendError: The turn could not be run — no committed manifest, an unreadable log directory, or a `_steward.yaml` that cannot be parsed and no history to fall back on.
        ManifestError: The committed manifest is not a manifest.
        ManifestVersionError: The manifest was captured by a different `task_identifier` version, so nothing in the log directory can be matched to it.
    """
    manifest = _manifest(workspace)

    if claim is not None:
        return _tend(
            workspace,
            manifest,
            claim,
            max_workers=max_workers,
            stall_after=stall_after,
            stuck_after=stuck_after,
            preauthorized=preauthorized,
            samples_ramp=samples_ramp,
            sync=sync,
            notification=notification,
            scan_model=scan_model,
        )

    outcome = acquire(workspace.claim, command="tend", break_stale=break_stale)
    if isinstance(outcome, Held):
        return Refused(held=outcome)

    with outcome as held:
        return _tend(
            workspace,
            manifest,
            held,
            max_workers=max_workers,
            stall_after=stall_after,
            stuck_after=stuck_after,
            preauthorized=preauthorized,
            samples_ramp=samples_ramp,
            sync=sync,
            notification=notification,
            scan_model=scan_model,
        )


def _tend(
    workspace: Workspace,
    manifest: Manifest,
    claim: Claim,
    *,
    max_workers: int | None,
    stall_after: int | None,
    stuck_after: int | None = None,
    preauthorized: dict[str, str] | bool | None = None,
    samples_ramp: tuple[int, int] | bool | None,
    sync: str | bool | None = None,
    notification: str | bool | None = None,
    scan_model: str | bool | None = None,
) -> TendResult:
    """One turn, with the claim already in hand however it got there."""
    # inside the claim, because resolving these can *write* — a degraded
    # `_steward.yaml` says so in `steward.log` — and a refused turn has to be
    # a genuine no-op. A timer firing every ten minutes against an agent's
    # long-held claim would otherwise leave a line each time it fired
    history = _history(workspace)
    settings = _settings(
        workspace,
        history,
        max_workers=max_workers,
        stall_after=stall_after,
        stuck_after=stuck_after,
        preauthorized=preauthorized,
        samples_ramp=samples_ramp,
        sync=sync,
        notification=notification,
        scan_model=scan_model,
        execute=True,
    )
    if claim.broke is not None:
        # machinery rather than a fact about the eval set, so it goes to
        # the operational log -- but it must be *somewhere*, because a
        # deterministic wedge is a kill loop and the only evidence of one
        # is a line per turn beside a `status.md` that never advances
        steward_log(
            workspace.log,
            f"broke a wedged claim held by pid {claim.broke.pid} "
            f"({claim.broke.command or 'unknown command'}, held since "
            f"{claim.broke.since or 'an unrecorded time'})",
        )
        # and in the journal too, because one break is recovery and a run of
        # them is the kill loop -- which only a fold across turns can see, and
        # `steward.log` is truncated. After the history read, deliberately: a
        # turn's own break is `claim.broke`, and counting it twice would fire
        # the loop item on the second break's first occurrence
        append_event(
            workspace.journal,
            ACTION,
            action="claim_broke",
            pid=claim.broke.pid,
            command=claim.broke.command,
        )
    return _turn(
        workspace,
        manifest,
        settings,
        history,
        execute=True,
        claim=None,
        broke=claim.broke,
    )


def status(
    workspace: Workspace,
    *,
    max_workers: int | None = None,
    stall_after: int | None = None,
    stuck_after: int | None = None,
    preauthorized: dict[str, str] | bool | None = None,
    samples_ramp: tuple[int, int] | bool | None = None,
) -> TendResult:
    """Report where the run stands, and what the next turn would do.

    `tend --dry-run`: the same reads and the same decision, with the actions discarded. That makes it a **preview** rather than a state dump — "6 tasks: 3 complete, 2 running, 1 errored; the next tend would launch 2 workers" is what both a human and an agent actually want to see before authorizing an interval.

    Not the cheap one, though. It performs the same reads; only the side effects are withheld.

    Args:
        workspace: The workspace to report on.
        max_workers: Worker processes to preview against.
        stall_after: Respawn patience to preview against.
        stuck_after: Stuck threshold to preview against.
        preauthorized: Standing rulings to preview against. The preview shows a matching class as ruled — the same decision the next tend will record — but nothing is journaled and nothing applied here; `False` previews with every standing grant declined.
        samples_ramp: Ramp envelope to preview against.

    Returns:
        What a turn would see and do.

    Raises:
        TendError: The state could not be read.
        ManifestError: The committed manifest is not a manifest.
        ManifestVersionError: The manifest cannot be matched against this inspect_ai.
    """
    history = _history(workspace)
    settings = _settings(
        workspace,
        history,
        max_workers=max_workers,
        stall_after=stall_after,
        stuck_after=stuck_after,
        preauthorized=preauthorized,
        samples_ramp=samples_ramp,
        execute=False,
    )
    manifest = _manifest(workspace)
    return _turn(
        workspace,
        manifest,
        settings,
        history,
        execute=False,
        claim=read_claim(workspace.claim),
        broke=None,
    )


@dataclass(frozen=True)
class _History:
    """What the journal says, read once and used for everything that asks.

    A turn used to read the journal only when `_steward.yaml` would not parse. It now has eight questions for it — the last good settings, the previous turn's items and when it happened, what has been acknowledged, what the agent has raised, how far anyone has collected, whether the run is paused, and what timer is armed — and they are one pass over the same events. The file is small by design (roughly sixty records a night, workflow.md §5.6) and the alternative is six reads of it per turn.
    """

    pool: Pool | None
    """Settings the most recent turn ran under, for degrading to a last known good."""

    previous: frozenset[str]
    """Item ids the most recent turn recorded, which is what this turn diffs against."""

    acknowledged: dict[str, Ack]
    """Items somebody has disposed of, by id."""

    complete: frozenset[str] | None = None
    """Display keys the most recent turn recorded as complete, or `None` where no turn recorded a set at all.

    **Absent and empty are different, and conflating them costs one wrong notification.** A run tended by a Steward that predates this key has no recorded set, and reading that as *nothing was complete* would make every already-finished task read as finishing this turn — one post naming two hundred tasks, on the first turn after an upgrade. `None` says *not known*, which suppresses the diff for exactly that turn and records a set for the next one.
    """

    stuck_after: int | None = None
    """The stuck threshold the most recent turn ran under, from the same recorded settings the pool degrades onto — a file that will not parse is exactly when a silently defaulted threshold would misreport the fleet."""

    raised: dict[str, Raised] = field(default_factory=dict[str, "Raised"])
    """Items the agent has handed to their owner, by id. Marked rather than removed — see `items.tend_items`."""

    collected: Collected | None = None
    """The most recent collection, or `None` where nobody has attached. What the collection age and the agent's delta are both computed from."""

    events: list[JournalEvent] = field(default_factory=list["JournalEvent"])
    """Every journal event, in file order.

    Carried whole rather than folded because the summary's *what happened* section is a filter over history rather than a fold of it, and the read has already happened. A fold per question would mean adding one every time that section admits another event type.
    """

    paused: Paused | None = None
    """The pause in force, or `None` where the run is scheduling normally."""

    armed: Armed | None = None
    """The timer the last arming installed, or `None`."""

    ever_armed: bool = False
    """Whether a timer was ever armed here. What distinguishes *never supervised* from *no longer supervised* (`items.Supervision`)."""

    ever_launched: bool = False
    """Whether anybody ever launched this run. The other half of the same distinction — see `items.Supervision.ever_launched`."""

    launched: str | None = None
    """When this run was most recently launched, or `None` where nothing ever launched it.

    The instant rather than the fact, because the acceptance latch releases on it: committing a manifest is the one moment desired state is decided, so a launch that re-asks for an accepted task is what puts it back in play (`_latched`).
    """

    signature: Signature | None = None
    """The most recent attestation, or `None` where nobody has signed this run."""

    since_tend: float | None = None
    """Seconds since the most recent recorded turn, or `None` where there has not been one."""

    since_armed: float | None = None
    """Seconds since the timer in force was armed, or `None` where none is."""

    baseline: Baseline = field(default_factory=Baseline)
    """The previous turn's tuning record — the window's left edge (`_tend.tuning`)."""

    ramp_holds: dict[str, RampHold] = field(default_factory=dict[str, "RampHold"])
    """The holds on the tuning loop, keyed by identifier with `""` for the fleet's."""

    ramp_levels: dict[str, int] = field(default_factory=dict[str, int])
    """Where the ramp has climbed each task, for respawns to start from."""

    last_step: dict[str, float] = field(default_factory=dict[str, float])
    """When each task's setpoint last moved, for the spacing gate."""

    damage: list[DamagedLine] = field(default_factory=list[DamagedLine])
    """Lines the journal read could not turn into events. Kept rather than discarded, because every fold above ran without whatever these lines said — and that is a fact about this turn's answers, not merely about the file (`items.JOURNAL_DAMAGE`)."""

    status_failing: str | None = None
    """When `status.md` stopped being writable, or `None` while it writes. The episode's opening edge, as `_write_status` journalled it — what tells this turn whether a failure is news and a success is a recovery."""

    fold_failing: str | None = None
    """When the scan fold started failing, or `None` while it folds. The same episode mechanics, and what keeps the fold being retried after the cheap `running or departed` gate has stopped firing (`_findings`)."""

    sync_failing: dict[str, str] = field(default_factory=dict[str, str])
    """Destinations the propagation has stopped reaching, each with when it stopped. The same episode mechanics as `status_failing`, per target."""

    breaks: int = 0
    """Consecutive turns that each had to break a wedged claim, as the journal records them. This turn's own break is not in it — the fold ran before the break was journalled — so the turn adds itself (`TendResult.breaks`)."""

    breaks_since: str | None = None
    """When the current run of breaks began, or `None` where there is none."""


def _history(workspace: Workspace) -> _History:
    """Read the journal once, answering everything a turn asks of it.

    Raises:
        TendError: The journal exists and could not be read. Refusing is the only honest answer: the journal holds the pause, the acknowledgments, and the last known settings, so a turn that proceeded on an empty history would silently un-pause the run, re-open every accepted decision, and degrade onto defaults nobody chose. The refusal reaches the channel through the same path every other failed turn does (`notify_failure`).
    """
    try:
        read = read_journal(workspace.journal)
    except OSError as ex:
        raise TendError(
            f"the journal ({workspace.journal}) could not be read: {ex} — a "
            f"turn cannot run without it, because it holds the pause, the "
            f"acknowledgments, and the settings a degraded turn falls back on"
        ) from ex

    events = read.events
    armed = read_armed(events)
    status_failing, fold_failing, sync_failing = _episodes(events)
    breaks_since, breaks = _breaks(events)
    ramp_levels, last_step = read_ramp_record(events)
    pool: Pool | None = None
    recorded_stuck: int | None = None
    previous: frozenset[str] | None = None
    complete: frozenset[str] | None = None
    since: float | None = None
    for event in reversed(events):
        if event.type != OBSERVATION:
            continue
        if previous is None:
            previous = frozenset(_strings(event.payload.get("items")))
            complete = _recorded(event.payload.get("complete"))
            since = _elapsed(event.ts)
        if pool is None:
            pool = _pool(event.payload.get("settings"))
            if pool is not None and isinstance(
                recorded := event.payload.get("settings"), dict
            ):
                recorded_stuck = _positive(
                    cast(dict[str, Any], recorded).get("stuck_after")
                )
        if pool is not None:
            break

    # what a failed post is still owed, taken off the baseline so that the next
    # diff produces it again. An edge is consumed by the observation that
    # records it, which is what stops a condition repeating -- and which would
    # otherwise make one unreachable minute at 2am cost the gate permanently
    owed_items, owed_complete = read_undelivered(events)
    launched = read_launched(events)
    return _History(
        pool=pool,
        stuck_after=recorded_stuck,
        previous=(previous if previous is not None else frozenset[str]()) - owed_items,
        complete=None if complete is None else complete - owed_complete,
        acknowledged=read_acks(events),
        raised=read_raised(events),
        collected=read_collected(events),
        events=events,
        paused=read_pause(events),
        armed=armed,
        ever_armed=any(event.type == ARMED for event in events),
        ever_launched=launched is not None,
        launched=launched,
        signature=read_signoff(events),
        since_tend=since,
        since_armed=_elapsed(armed.ts) if armed is not None else None,
        baseline=read_baseline(events),
        ramp_holds=read_ramp_holds(events),
        ramp_levels=ramp_levels,
        last_step=last_step,
        damage=read.damage,
        status_failing=status_failing,
        fold_failing=fold_failing,
        sync_failing=sync_failing,
        breaks=breaks,
        breaks_since=breaks_since,
    )


def _episodes(
    events: list[JournalEvent],
) -> tuple[str | None, str | None, dict[str, str]]:
    """The failures in force: `status.md`'s write, the scan fold, and each sync destination's.

    An episode opens with the `action` its writer journals on the *first* failure and closes with the `…_restored` its first success writes, so the fold is a switch per subject and the answer is *when it started* — which is what keys the item, and what makes acknowledging one episode not cover the next. Defensive on doubled edges (a crash can repeat one): the episode keeps its original start.
    """
    status_failing: str | None = None
    fold_failing: str | None = None
    sync_failing: dict[str, str] = {}
    for event in events:
        if event.type != ACTION:
            continue
        action = event.payload.get("action")
        if action == "status_unwritable":
            status_failing = status_failing or event.ts
        elif action == "status_unwritable_restored":
            status_failing = None
        elif action == SCAN_FOLD_FAILED:
            fold_failing = fold_failing or event.ts
        elif action == SCAN_FOLD_RESTORED:
            fold_failing = None
        elif action in ("sync_failed", "sync_restored"):
            target = event.payload.get("target")
            if not isinstance(target, str) or not target:
                continue
            if action == "sync_failed":
                sync_failing.setdefault(target, event.ts)
            else:
                sync_failing.pop(target, None)
    return status_failing, fold_failing, sync_failing


def _breaks(events: list[JournalEvent]) -> tuple[str | None, int]:
    """The run of consecutive turns that each had to break a wedged claim.

    Counted per `claim_broke` rather than per turn-slot, because the turns that matter most never reach their observation: a tend that breaks its predecessor and then wedges itself leaves only the break behind, and the next break lands in the same slot. What ends the run is the one thing a loop cannot produce — an observation from a turn that broke nothing.

    Returns:
        When the run began and how many breaks it holds, `(None, 0)` where the last completed turn was clean.
    """
    since: str | None = None
    count = 0
    broke_this_slot = False
    for event in events:
        if event.type == ACTION and event.payload.get("action") == "claim_broke":
            if count == 0:
                since = event.ts
            count += 1
            broke_this_slot = True
        elif event.type == OBSERVATION:
            if not broke_this_slot:
                since, count = None, 0
            broke_this_slot = False
    return since, count


def _elapsed(ts: str) -> float | None:
    """Seconds from a recorded instant until now, or `None` where it cannot be read.

    Unparseable rather than absent: a journal written by a version that stamped its timestamps differently is history, not damage, and the caller's answer to *how long since the last tend* is then *unknown* rather than *forever*.
    """
    return seconds_since(ts)


def _pool(recorded: object) -> Pool | None:
    """The settings an `observation` payload recorded, if it recorded usable ones.

    **`stall_after` is what says the payload is one.** It is the only setting with a default rather than an unbounded `None`, so it is the only one whose absence distinguishes *this turn recorded no settings* from *this turn recorded no limit* — a distinction that used to ride on `max_workers` and stopped being available the moment that key could legitimately be null.
    """
    if not isinstance(recorded, dict):
        return None
    settings = cast(dict[str, Any], recorded)
    if (stall_after := _positive(settings.get("stall_after"))) is None:
        return None
    return Pool(
        max_workers=_positive(settings.get("max_workers")),
        max_tasks=_positive(settings.get("max_tasks")),
        max_samples=_positive(settings.get("max_samples")),
        samples_ramp=_ramp(settings.get("samples_ramp")),
        stall_after=stall_after,
    )


def _ramp(recorded: object) -> tuple[int, int] | Literal[False] | None:
    """The `samples_ramp` a settings payload recorded, if it recorded a usable one.

    Part of the degrade path, for the same reason the rest of the payload is: an operator who disabled ramping and then broke `_steward.yaml` with an edit must not have a fleet start climbing on Steward's default — that is exactly the *further into a provider than anyone chose* the fallback exists to prevent.
    """
    if recorded is False:
        return False
    if isinstance(recorded, list):
        entries = cast(list[object], recorded)
        if (
            len(entries) == 2
            and all(
                isinstance(entry, int) and not isinstance(entry, bool)
                for entry in entries
            )
            and 0 < cast(int, entries[0]) <= cast(int, entries[1])
        ):
            return (cast(int, entries[0]), cast(int, entries[1]))
    return None


def _strings(value: object) -> list[str]:
    """A list of strings from a journal payload, which may hold anything."""
    if not isinstance(value, list):
        return []
    return [entry for entry in cast(list[object], value) if isinstance(entry, str)]


def _recorded(value: object) -> frozenset[str] | None:
    """The same, keeping *the key was not there* distinct from *it was empty*.

    `_strings` answers what a payload said; this answers whether it said anything, which is the question a diff has to ask before it can trust its own left-hand side (`_History.complete`).
    """
    if not isinstance(value, list):
        return None
    return frozenset(_strings(cast(object, value)))


def _finished(progress: Progress) -> list[str]:
    """Identifiers of the tasks that are complete, sorted.

    One definition used by both the diff and the record it is diffed against, so the two cannot come to disagree about what *finished* counts as. An orphan is not in it: its state is `ORPHANED` whatever its log says, and a log the current definition does not ask for is not this run's task finishing.

    **Identifiers rather than display keys, because this outlives the turn that wrote it.** A display key is computed against the tasks on screen and `ManifestTask.key` says so: relaunching with a task that collides on name gives an already-complete `swe_bench` the longer key `swe_bench@openai/gpt-5`, which the next diff reads as a task that finished tonight. The identifier is what does not move. The rendering maps back to display keys, since nobody wants to read a digest at 2am (`_notify._lines`).
    """
    return sorted(
        row.identifier for row in progress.rows if row.state is TaskState.COMPLETE
    )


@dataclass(frozen=True)
class _Settings:
    """What this turn is operating under, and whether that is the file's own answer."""

    pool: Pool
    degraded: str | None
    degraded_at: str | None = None
    """What keys the degraded item, so that a second, different failure is heard rather than covered by the first acknowledgment.

    `_steward.yaml`'s modification time where the *file* would not parse, so an edit that still fails is heard again. Where the **environment** is what failed there is no file to stamp — the same `_steward.yaml`, unedited, would have stamped two different broken variables identically and let an acknowledgment of one suppress the other — so it is a fingerprint of the refusal itself.
    """

    interval: int | None = None
    """How often `_steward.yaml` asks to be tended, or `None` where it does not ask.

    Not something a turn acts on, and deliberately the *expressed* preference rather than the resolved one — it exists to be compared against what is actually armed, and a comparison against Steward's own default would report drift from a number nobody wrote.
    """

    sync: str | bool | None = None
    """Where the workspace propagates to, as `_steward.yaml` or the environment expressed it.

    Unresolved, because resolving it needs the log directory and that is `_turn`'s to compute. `None` here means *no preference*, which resolves to the log directory rather than to nowhere.
    """

    notification: str | bool | None = None
    """Where Steward's own spellings say to post, `False` for nowhere, or `None` where they say nothing.

    Unresolved, like `sync` and for the same reason: the fourth rung is `INSPECT_EVAL_NOTIFICATION`, and reading it belongs to `_notify.channel`, which owns both directions of the reflexive relationship. Carried through a degraded turn too — a `_steward.yaml` that will not parse is among the conditions most worth telling somebody about, and the channel must not be the second casualty of the same file.
    """

    channel: str | bool | None = None
    """What the *workspace* says, whatever a flag said about Steward posting.

    The two come apart in exactly one case and it is the one that matters: `--no-notification` beside a `notification:` in `_steward.yaml` silences Steward and must still reach the fleet, because a worker's notifications are blocking prompts (`_notify.channel.establish_channel`). Everywhere else this is the same value as `notification`.
    """

    scan_model: str | bool | None = None
    """The model scanners use, `False` for none configured, or `None` where the spellings say nothing.

    Unresolved, like `notification` and on its pattern exactly: the last rung is `SCOUT_SCAN_MODEL`, and reading it belongs to `_scan.model`, which owns both directions of that reflexive relationship. Through a degraded turn the fleet keeps scanning, but with whatever spellings survive: a `_steward.yaml` that will not parse takes its `scan_model:` down with it, and a scheduled turn — no flag to say otherwise — falls to `STEWARD_SCAN_MODEL`, then the ambient default. Accepted rather than engineered around: the ambient default is each sample's own model, and a broken file is already the turn's headline.
    """

    policies: list[str] = field(default_factory=list[str])
    """The standing rules in force, from whichever source expressed them.

    Carried rather than acted on, and reported rather than interpreted. It exists because `policies` can now arrive from `STEWARD_POLICIES` as readily as from the file, which makes *open `_steward.yaml`* an incomplete instruction for an agent — so the turn has to be able to say what is actually in force. A block scalar arrives as one entry, since splitting somebody's paragraphs on their behalf would be interpreting them.
    """

    stuck_after: int | None = None
    """Seconds of sample silence before a `stuck` item, or `None` for `DEFAULT_STUCK_AFTER`. On a degraded turn, the last recorded value — the same last-known-good the pool falls back on."""

    stuck_cancel: bool | list[str] | None = None
    """Which stuck tool calls the agent may cancel, as the file expressed it. `None` on a degraded turn whose file would not parse — a standing authority whose text cannot be read is not guessed from history."""

    preauthorized: dict[str, str] | None = None
    """The rulings granted in advance, as patterns to dispositions. `None` on a degraded turn for the same reason as `stuck_cancel` — degrading must narrow authority, never preserve it."""

    log_store: str | None = None
    """The reuse store this workspace configures, or `None` for none.

    **Resolved rather than expressed**, unlike `interval` and `sync`, because the only thing that reads it here is a sentence naming a location to a person — and *which store* is exactly what the precedence exists to answer. Carried for the readiness item alone: publication is the one act at signoff that nothing turns on by default, so the invitation has to say there is a decision waiting or nobody is ever asked to make one.
    """


def _turn(
    workspace: Workspace,
    manifest: Manifest,
    settings: _Settings,
    history: _History,
    *,
    execute: bool,
    claim: Held | None,
    broke: Held | None,
) -> TendResult:
    """Both dispositions, differing only in whether the actions are carried out."""
    # first, because it is what puts the channel into the environment every
    # worker this turn spawns inherits -- and because the two halves have to be
    # settled together: a fleet posting somewhere Steward is not is the silent
    # divergence `_notify.channel` exists to prevent. Harmless on a `status`,
    # which mutates only its own environment and posts nothing
    channel = establish_channel(
        workspace, notification=settings.notification, fleet=settings.channel
    )
    # described immediately after it is settled, and from the same two values
    # that settled it: the snapshot has to be able to say whether anything will
    # reach a person, and the spellings that answer that are unreadable from
    # outside this process (`_notify.Channel`)
    notification = describe_channel(
        target=channel,
        notification=settings.notification,
        channel=settings.channel,
    )
    # beside the channel and for its reason: this is the other value every
    # worker this turn spawns inherits from this process's environment, and
    # settling it late would leave part of a fleet scanning with the shell's
    # answer rather than the workspace's
    establish_scan_model(settings.scan_model)
    drift = _drifted(workspace, manifest)
    log_dir = _log_dir(workspace, manifest)

    inflight = resolve_inflight(workspace.inflight, workspace.workers)
    # read even for a `status`, which is the disposition that most needs it: a
    # person types it to satisfy their curiosity, and a settled directory of two
    # thousand logs is two thousand header reads it can skip entirely
    cache = read_attempt_cache(workspace.observed)
    try:
        logs = observe_logs(log_dir, cache=cache)
    except OSError as ex:
        # scheduling into a directory that cannot be read would multiply the
        # loss; running workers are left alone, since one that still holds its
        # own handle may finish normally (execution.md, *When the substrate fails*)
        if execute:
            steward_log(
                workspace.log, f"could not read the log directory {log_dir}: {ex}"
            )
        raise TendError(
            f"the log directory {log_dir} could not be read, so nothing can be "
            f"scheduled against it: {ex}"
        ) from ex

    observed = observe_tasks(manifest, logs)
    # read, never `resolve_eval_set_id`, which mints and *writes* one — a
    # `status` must leave the log directory exactly as it found it, and a
    # directory with no id has had no fleet and so no rows. Settled once for
    # the two readers of it, the fold below and signoff's finalize
    scan_id = existing_eval_set_id(log_dir) or manifest.eval_set_id
    # the anomaly and applied folds are hoisted above the decision, because the
    # decision consumes them: reconcile forgives attempt history at a rerun
    # ruling's instant and schedules the authorized re-runs first, so it needs
    # the rulings in force before it decides. Pure folds over events already
    # read -- hoisting them costs nothing
    anomalies = read_anomalies(history.events)
    applied = read_applied(history.events)
    decision = reconcile(
        manifest,
        inflight,
        observed,
        pool=settings.pool,
        paused=history.paused is not None,
        levels=history.ramp_levels,
        ruled=rerun_ruled(anomalies),
        accepted=_latched(anomalies, history.launched),
        # the definition's number where it declared one, else what the last turn
        # read back off the workers -- this turn's live read happens below, and
        # a spawn decided here cannot wait for it
        budget=_positive(manifest.options.get("max_sandboxes"))
        or history.baseline.budget,
    )
    # one read of the running fleet, feeding both the table's live columns and
    # the block under it -- a second read would be a second set of numbers, and
    # a row saying `83 running` beside a block saying nothing is running is the
    # kind of disagreement a reader has no way to resolve
    fleet = _live(
        inflight,
        logs,
        stuck_after=(
            settings.stuck_after
            if settings.stuck_after is not None
            else DEFAULT_STUCK_AFTER
        ),
    )

    # the anomaly census, diffed against the journal's fold. The pending
    # events are computed for both dispositions and folded in for both --
    # `anomalies` below is the state the journal holds after this tend, and
    # the state it *would* hold for a status -- but only an executing turn
    # appends them (below, before the observation)
    classed = read_classed_cache(workspace.classed)
    found = _findings(
        workspace,
        manifest,
        observed,
        logs,
        inflight,
        log_dir,
        scan_id,
        execute=execute,
        fold_failing=history.fold_failing,
    )
    detection = detect(
        observed,
        logs,
        inflight,
        fleet,
        workers_dir=workspace.workers,
        cache=classed,
        findings=found.instances,
    )
    if detection.unreadable or found.unreadable:
        # summaries damage joins the header damage on the item surface; the
        # summary's count was taken by reconcile before this read and stays a
        # count of headers. Scan rows that would not read join them there too:
        # the question *what could this run not see* has one answer, and the
        # signoff gate refuses on it whichever file it was
        observed = replace(
            observed,
            unreadable=[
                *observed.unreadable,
                *detection.unreadable,
                *found.unreadable,
            ],
        )
    # one read, two readers: the uuid set a resumed task's current log actually
    # holds is both coverage's denominator and the narrowing every scan-shaped
    # report needs -- and computing it twice is how the two come to disagree
    reused, unverified = reused_samples(observed, found)
    scanned = coverage(
        observed,
        found.recorded,
        reused=reused,
        unverified=unverified,
        scanning=bool(manifest.scan is not None and scan_id is not None),
    )
    pending = absorb(anomalies, detection.batches, task_health(observed), applied)
    if pending:
        anomalies = read_anomalies([*history.events, *as_events(pending, utc_now())])
    # standing pre-authorizations are part of the turn's decision, so they are
    # computed here on the shared path: a `status` folds the would-be rulings
    # state-if-executed exactly as it folds pending windows -- a class the
    # next tend will auto-rule must not preview as an open question -- while
    # only an executing turn journals them (and then refolds from the file,
    # because a ruling's recorded instant is its identity)
    # the narrowed per-class counts, so a policy's composed effect sentence
    # says what a person's would (`rulings.affected_refs`)
    affected = affected_refs(detection.batches, _current_locations(observed), reused)
    policy, declined = policy_rulings(anomalies, settings.preauthorized, affected)
    if policy:
        anomalies = read_anomalies(
            [
                *history.events,
                *as_events(pending, utc_now()),
                *as_events(policy, utc_now()),
            ]
        )
    progress = Progress(
        rows=task_progress(observed, fleet, scanned.by_task),
        # the pids come from the in-flight record rather than from the fleet,
        # because they answer a different question: a worker too busy to serve
        # its socket, and one that has not bound one yet, are both costing the
        # machine memory right now and neither appears in `fleet` as answered
        live=live_totals(fleet, [worker.pid for worker in inflight.running]),
    )
    answered = _signals(observed, fleet)
    plan = plan_tuning(
        answered,
        ramp=resolve_samples_ramp(manifest, settings.pool),
        budget=_positive(manifest.options.get("max_sandboxes")),
        baseline=history.baseline,
        holds=history.ramp_holds,
        last_step=history.last_step,
        cpu=progress.live.usage.seconds if progress.live is not None else {},
        now=datetime.now(timezone.utc).timestamp(),
        absent=inflight.running_identifiers - {task.identifier for task in answered},
    )
    if history.paused is not None:
        # a paused run makes no changes to itself, and a retune is a change --
        # but the record survives, so the window is continuous across a pause
        # rather than the first turn after a resume measuring against a
        # baseline from before it
        plan = replace(plan, moves=[], proposals=[], lines=[])

    result = TendResult(
        summary=decision.summary,
        queued=decision.queued,
        drift=drift.changed,
        degraded=settings.degraded,
        claim=claim,
        broke=broke,
        definition_hash=drift.digest,
        manifest_digest=manifest_digest(manifest),
        degraded_at=settings.degraded_at,
        policies=settings.policies,
        log_store=settings.log_store,
        supervision=Supervision(
            armed=history.armed,
            ever_armed=history.ever_armed,
            ever_launched=history.ever_launched,
            interval=settings.interval,
            since_tend=history.since_tend,
            since_armed=history.since_armed,
        ),
        executed=execute,
        progress=progress,
        tuning=plan,
        log_dir=log_dir,
        notification=notification,
        scan=manifest.scan,
        scan_id=scan_id,
        coverage=scanned,
        fold_failing=history.fold_failing,
        journal_damage=history.damage,
        status_failing=history.status_failing,
        sync_failing=history.sync_failing,
        # this turn's own break is not in the fold -- the history was read
        # before the break was journalled -- so it is added here, once
        breaks=history.breaks + (1 if broke is not None else 0),
        breaks_since=history.breaks_since,
        anomalies=anomalies,
        anomaly_pending=pending,
        dispositions=dispositions(
            detection.batches, anomalies, _current_locations(observed), reused
        ),
        stuck_cancel=_cancel_authority(settings.stuck_cancel),
        observed=observed,
        acknowledged=dict(history.acknowledged),
        current_logs=_current_locations(observed),
        caveats=caveats(
            anomalies,
            history.acknowledged,
            detection.batches,
            {task.identifier: task.key for task in observed.tasks},
            _current_locations(observed),
            _cleared(observed),
            reused,
        ),
        signature=history.signature,
        launched=history.launched,
    )
    # the co-authored document's facts, composed before either disposition
    # branches: a `status` has to report what is unwritten as surely as a tend
    # does, and what is unwritten depends on the sections this turn would add.
    # Composed off the pre-journalling fold, which carries the same rulings the
    # refold below re-reads -- only their recorded instants differ, and no fact
    # here reads one.
    #
    # **This merge is read for its report and thrown away.** The write happens
    # at the end of the turn and merges again, against the file as it stands
    # *then* -- see `_write_analysis`
    sections = analysis_sections(result)
    if (authored := _read_authored(workspace.analysis)) is not None:
        result = replace(result, unwritten=merge_analysis(authored, sections).unwritten)
    if not execute:
        return _projected(result, observed, inflight, history)

    # the stall guard's account of the instants it could not read. Machinery
    # rather than the run, so it goes to the operational log -- and only on an
    # executing turn, which is what keeps `reconcile` pure and `status` silent
    for warning in decision.warnings:
        steward_log(workspace.log, warning)

    acted = _act(workspace, manifest, log_dir, decision.actions, observed)

    # the anomaly deltas land now, after the acting they describe alongside and
    # before anything that decides against them -- a policy ruling appended
    # ahead of its window's `opened` would be skipped by every later fold, the
    # exact trap `persist_windows` closes for the verbs. An append that fails
    # fails the turn, exactly like the observation's own: the journal is the
    # one record nothing can rebuild, and the next turn's diff re-derives
    # whatever did not land rather than double-counting what did
    for entry in pending:
        append_event(workspace.journal, entry.type, **entry.fields)

    # the standing pre-authorizations the shared path computed become ordinary
    # rulings now, before the applier reads the fold, so a pattern's ruling
    # lands and applies in one turn. Reused rather than recomputed: the shared
    # path already folded them into `anomalies` state-if-executed, and asking
    # again against that state would find the windows already ruled and
    # journal nothing
    for note in declined:
        steward_log(workspace.log, note)
    if policy:
        for entry in policy:
            append_event(workspace.journal, entry.type, **entry.fields)
        acted.journalled = True
        # refold from the journal itself, never from re-synthesized events: a
        # ruling's ts is identity (the applied fold keys on it), and only the
        # file holds the instant `append_event` actually stamped -- a second
        # `utc_now()` here would make `ruling_applied.for` name a ruling the
        # journal does not contain, and the next turn would apply it again
        anomalies = read_anomalies(read_journal(workspace.journal).events)
        result = replace(
            result,
            anomalies=anomalies,
            dispositions=dispositions(
                detection.batches, anomalies, _current_locations(observed), reused
            ),
        )

    # the executor: warm requeues and landed invalidations, applied against the
    # pre-application census -- outcomes are next turn's observation, the same
    # one-turn lag every other effect has
    apply_rulings(
        workspace,
        anomalies,
        detection.batches,
        applied,
        inflight,
        fleet,
        observed,
        spawned={
            task.identifier
            for action in decision.actions
            if isinstance(action, SpawnWorker)
            for task in action.tasks
        },
        acted=acted,
    )
    retuned = _retune(workspace, plan, acted)

    if acted.journalled:
        # the projection below reports *what has been done to this run*, and
        # this turn has just done something to it -- so the read that fed it is
        # already out of date. `status.md` saying "nothing has been done to this
        # run yet" beside an archive it performed a millisecond earlier is the
        # summary contradicting its own side effects, and the entry would not
        # surface until some later turn happened to read the file again.
        #
        # Only `events` is replaced, and only on the turns that wrote: nothing
        # an action appends is an ack, a hand-off, or a collection, so every
        # other fold in this history is still the answer it was
        history = replace(history, events=_reread(workspace, history.events))
    result = _projected(
        replace(
            result,
            spawned=acted.spawned,
            reaped=acted.reaped,
            archived=acted.archived,
            failures=acted.failures,
        ),
        observed,
        inflight,
        history,
    )

    # narrowed to what this listing named *minus what this turn just moved out
    # of it*, which is what keeps it bounded. The subtraction is because the
    # listing was taken before the archiving; without it every archived log
    # would linger one extra turn. An archive that failed simply costs a header
    # read next turn, which is why this does not consult `acted`.
    #
    # **A tend writes this and a status does not.** The cache is disposable and
    # a write of it mutates nothing about the run, so the rule is not really
    # about damage -- it is that *writes nothing* is a promise worth being able
    # to make without a footnote, and the tend on the timer keeps the cache warm
    # for whoever types `status` between turns anyway.
    moved = {
        action.location for action in decision.actions if isinstance(action, ArchiveLog)
    }
    write_attempt_cache(workspace.observed, cache.keep({*logs.locations} - moved))
    # the classification cache follows the same discipline, narrowed the same
    # way, plus to the evals still running (its per-sample memo keys on them)
    write_classed_cache(
        workspace.classed,
        classed.keep(
            {*logs.locations} - moved,
            running={
                attempt.eval_id
                for attempts in logs.attempts.values()
                for attempt in attempts
                if attempt.status == "started"
            },
        ),
    )

    # before the observation, so a crash between the two costs a repeated
    # observation rather than a snapshot the journal claims was written -- and
    # after the executor, so a caveat carried out this turn is in the document
    # this turn rather than one behind the effect it describes
    _write_status(workspace, result, failing_since=history.status_failing)
    _write_anomalies(workspace, result)
    _write_analysis(workspace, result, sections)
    _record(
        workspace,
        result,
        pool=settings.pool,
        stuck_after=settings.stuck_after,
        tuning=observation_payload(plan, retuned),
    )
    _sync(workspace, settings, log_dir, failing=history.sync_failing)
    # last, after the file a post's footer sends its reader to has been written
    # and propagated -- and never at the cost of the turn, which has already
    # happened by the time anything is said about it
    notify_turn(workspace, result, channel)
    return result


def _signals(observed: ObservedTasks, fleet: LiveFleet) -> list[TaskSignals]:
    """The tuning policy's per-task inputs, in table order.

    Only the rows a worker answered for, because everything the policy gates on is a live reading — a task whose worker is busy simply contributes no window this turn, which the gates read as *not known to be clean* and wait out.
    """
    rows: list[TaskSignals] = []
    for task in observed.tasks:
        live = fleet.tasks.get(task.identifier)
        if live is None or live.unavailable is not None or not live.task_id:
            continue
        rows.append(signals(task.key, live))
    return rows


def _retune(workspace: Workspace, plan: TuningPlan, acted: "_Acted") -> list[Move]:
    """Carry out the tuning moves, and journal each one that lands.

    The same posture as `_act`: one failing retune does not fail the turn or the moves after it, and the journal entry is written **after** the control channel accepts the change — an entry describing a retune that never happened would poison the very fold that decides what a respawn starts at.
    """
    applied: list[Move] = []
    for move in plan.moves:
        described = f"retune {move.key} ({move.knob} {move.at}→{move.to})"
        try:
            outcome = task_config(
                move.task_id,
                max_samples=move.to if move.knob == "max_samples" else None,
                max_connections=move.to if move.knob == "max_connections" else None,
                reason=move.reason,
            )
        except RuntimeError as ex:
            # a usage error -- Steward built a line the CLI does not accept.
            # A defect here rather than a condition out there, and it must
            # cost this move rather than the turn: unwrapped it failed the
            # whole tend, losing the observation, the snapshot, and the post,
            # every interval, with nothing saying so anywhere but a traceback
            # nobody was standing at
            _failed(workspace, acted, f"could not {described}", ex)
            continue
        if isinstance(outcome, Unavailable):
            failure = f"could not {described}: {outcome.kind}: {outcome.detail}"
            acted.failures.append(failure)
            steward_log(workspace.log, failure)
            continue
        if not outcome.applied:
            warned = "; ".join(outcome.warnings) or "the change was not applied"
            failure = f"could not {described}: {warned}"
            acted.failures.append(failure)
            steward_log(workspace.log, failure)
            continue
        if (outcome.persisted or {}).get(move.knob) is False:
            # the retune is live and stays live -- undoing a change that worked
            # because its receipt did not get filed would be the wrong repair.
            # But one of the three records the ramp promises is missing, and an
            # unattended retune nobody can find afterwards is the thing that
            # provenance exists to prevent, so it is reported rather than
            # inferred later from a gap
            unfiled = (
                f"{described} took effect but was not recorded in the eval log; "
                f"the journal has it and the log will not"
            )
            acted.failures.append(unfiled)
            # `steward.log` too, like every other failure in this loop: the
            # failures list feeds a single turn's items, and the operational
            # log is where a reader reconstructs the machinery's night from
            steward_log(workspace.log, unfiled)
        append_event(
            workspace.journal,
            ACTION,
            action="ramp",
            knob=move.knob,
            identifier=move.identifier,
            task=move.key,
            at=move.at,
            to=move.to,
            reason=move.reason,
        )
        acted.journalled = True
        applied.append(move)
    return applied


def _reread(workspace: Workspace, previous: list[JournalEvent]) -> list[JournalEvent]:
    """The journal again, or what was already read where it cannot be.

    Falling back rather than raising, and rather than falling back to nothing: the turn has already happened, and the cost of a failed re-read is one section of one document missing an entry until the next turn. Returning an empty list instead would report the whole night as never having happened.
    """
    try:
        return read_journal(workspace.journal).events
    except OSError:
        return previous


def _projected(
    result: TendResult,
    observed: ObservedTasks,
    inflight: InFlight,
    history: _History,
) -> TendResult:
    """Fill in the items, the verdict, and what changed since the last turn.

    Last, because an item can be about something the acting produced — an action that failed is a fact about this turn, not about the directory it read. Which also makes the two dispositions honest against each other: a `status` projects a turn that did nothing, so it reports what is open *now* rather than what would be open afterwards.
    """
    # the collection stamp first, because two readers below need it: the hold's
    # attachment test, and the item that asks an agent for a write-up — which
    # must not be raised in a workspace no agent was ever attached to
    result = replace(
        result,
        happened=happened(history.events),
        collected=history.collected,
        since_collected=(
            _elapsed(history.collected.ts) if history.collected is not None else None
        ),
        position=max((event.line for event in history.events), default=0),
    )
    items = tend_items(
        result,
        observed,
        inflight,
        frozenset(history.acknowledged),
        frozenset(history.raised),
    )
    current = {item.id for item in items}
    held = held_tasks(result, spent=history.complete or frozenset())
    return replace(
        result,
        items=items,
        held=held,
        verdict=verdict(
            items,
            paused=result.summary.paused,
            running=result.summary.running,
            spawning=result.summary.spawning,
            unfinished=unfinished(result.summary, result.acknowledged),
            # tasks with nothing left running, which is what makes a park
            # subtract from progress rather than merely accompany it. A task
            # with one sample parked among fifty working is still progressing
            parked=sum(
                1
                for row in result.progress.rows
                if row.parked.total and row.parked.total >= row.running
            ),
            signed=result.signed,
        ),
        appeared=sorted(current - history.previous),
        resolved=sorted(history.previous - current),
        finished=(
            sorted(frozenset(_finished(result.progress)) - history.complete - held)
            if history.complete is not None
            else []
        ),
    )


@dataclass
class _Acted:
    """What actually happened, which is not always what was decided."""

    spawned: list[str] = field(default_factory=list[str])
    reaped: list[str] = field(default_factory=list[str])
    archived: list[str] = field(default_factory=list[str])
    failures: list[str] = field(default_factory=list[str])

    journalled: bool = False
    """Whether any of this landed in the journal.

    Set explicitly rather than inferred from `archived`, which is the only action that writes one today. The turn re-reads the journal when this is true, so an action added later that appends and forgets to set this would quietly reintroduce a summary that omits its own side effects.
    """


def _act(
    workspace: Workspace,
    manifest: Manifest,
    log_dir: str,
    actions: list[Action],
    observed: ObservedTasks,
) -> _Acted:
    """Carry out a turn's actions, in the order `reconcile` put them in.

    **One failing action does not fail the turn.** A spawn that cannot start a process, or a log that cannot be moved, is recorded and stepped over — the remaining actions are independent of one another, and the next turn decides again from what it finds rather than from what was attempted. Aborting instead would let one bad task hold up an entire fleet, every ten minutes, forever.
    """
    acted = _Acted()
    spawns: list[SpawnWorker] = []
    # identifier to display key, because the journal is read by a person: an
    # identifier is ~200 characters with two hashes in it, and an entry naming
    # one is an entry nobody reads. Both go into the payload — the key to be
    # read, the identifier to be matched against.
    #
    # **Wanted and not done, rather than merely not complete.** An orphan is
    # neither: the manifest stopped asking for it, so a worker of its leaving
    # is not work left undone, and this turn is archiving its log rather than
    # picking the task back up
    unfinished = {
        task.identifier: task.key
        for task in observed.tasks
        if task.state in (TaskState.MISSING, TaskState.INCOMPLETE)
    }
    # what is actually being picked up again this turn, so the entry can say so
    # only where it is true. A departure on the turn the stall guard trips is
    # reaped and *not* respawned, and promising otherwise sends a reader
    # looking for a worker that was never going to start
    retrying = {
        task.identifier
        for action in actions
        if isinstance(action, SpawnWorker)
        for task in action.tasks
    }

    for action in actions:
        if isinstance(action, SpawnWorker):
            # held back so the fleet is built once and only if there is
            # something to spawn. `reconcile` already orders spawns last, so
            # collecting them here preserves that order rather than imposing it
            spawns.append(action)
        else:
            _carry_out(workspace, log_dir, action, acted, unfinished, retrying)

    if spawns:
        _spawn_all(workspace, manifest, log_dir, spawns, acted)
    return acted


def _carry_out(
    workspace: Workspace,
    log_dir: str,
    action: ReapWorker | ArchiveLog,
    acted: _Acted,
    unfinished: dict[str, str],
    retrying: set[str],
) -> None:
    """Do one thing that is not a spawn, and survive it not working."""
    try:
        match action:
            case ReapWorker():
                record_exited(workspace.inflight, worker=action.worker.worker)
                acted.reaped.append(action.worker.worker)
                _record_departure(workspace, action, unfinished, retrying, acted)

            case ArchiveLog():
                # journalled after the move, and carrying where the log
                # actually went: an entry describing an archive that never
                # happened is a lie in the one record nothing can rebuild,
                # where a missing entry is recoverable by looking
                destination = archive_log(action.location, log_dir)
                append_event(
                    workspace.journal,
                    ACTION,
                    action="archive",
                    reason="orphaned",
                    identifier=action.identifier,
                    location=action.location,
                    archived=destination,
                )
                acted.archived.append(destination)
                acted.journalled = True
    except Exception as ex:
        _failed(workspace, acted, _describe(action), ex)


def _record_departure(
    workspace: Workspace,
    action: ReapWorker,
    unfinished: dict[str, str],
    retrying: set[str],
    acted: _Acted,
) -> None:
    """Journal a worker that went away with work still to do.

    **Only the ones that did not finish, and that narrowing is the admission test rather than a saving.** A worker exits at the end of every task, so recording every reap would put a line in *what happened* for each task that completed — which is the run happening, not something that happened to the run (`history.py`). A worker that exits with its task unfinished is the other thing entirely: nothing asked it to stop, its work is being repeated, and it is the single event a reader of an overnight history most needs and currently has no other way to learn.

    **The observation is a reliable verdict on it**, because the reap and the read are ordered: `resolve_inflight` decided this worker was gone before `observe_logs` read the directory, so its log had already settled by then.

    The respawn is not recorded as its own entry — Steward converging is the normal thing it does — but *whether* one was decided is, because the two cases read completely differently to somebody working through a night. A departure this turn picks back up is a hiccup; one it does not is either a run at its width or a task the stall guard has given up on, and both are things to go and look at.
    """
    departed = action.worker
    stranded = sorted(set(departed.identifiers) & set(unfinished))
    if not stranded:
        return
    append_event(
        workspace.journal,
        ACTION,
        action="reap",
        # never launched is a different diagnosis from died mid-task, and the
        # record cannot recover the distinction later -- a worker with no pid
        # is one whose intent was written and whose spawn never returned
        reason="never_started" if departed.pid is None else "died",
        worker=departed.worker,
        pid=departed.pid,
        tasks=[unfinished[identifier] for identifier in stranded],
        identifiers=stranded,
        retrying=all(identifier in retrying for identifier in stranded),
    )
    acted.journalled = True


def _spawn_all(
    workspace: Workspace,
    manifest: Manifest,
    log_dir: str,
    spawns: list[SpawnWorker],
    acted: _Acted,
) -> None:
    """Spawn every worker this turn decided on.

    The fleet is built once, and a failure to build one is reported **once** rather than per spawn: it resolves the definition and checks the packages its type needs, so when it fails it fails identically for every task, and forty copies of one message is not forty pieces of information.
    """
    try:
        fleet = _fleet(workspace, manifest, log_dir)
    except Exception as ex:
        _failed(workspace, acted, "could not prepare to spawn workers", ex)
        return

    for action in spawns:
        try:
            acted.spawned.append(fleet.spawn(action).worker)
        except Exception as ex:
            _failed(workspace, acted, _describe(action), ex)


def _failed(workspace: Workspace, acted: _Acted, what: str, ex: Exception) -> None:
    """Record an action that did not work, in both places it belongs.

    The exception's type is named as well as its message, because a bare message from an unexpected failure is often unattributable — and this path deliberately catches everything, so an ordinary bug can land here too.
    """
    failure = f"{what}: {type(ex).__name__}: {ex}"
    acted.failures.append(failure)
    steward_log(workspace.log, failure)


def _describe(action: Action) -> str:
    match action:
        case ReapWorker():
            return f"could not reap {action.worker.worker}"
        case ArchiveLog():
            return f"could not archive {action.location}"
        case SpawnWorker():
            return f"could not spawn {action.first.key}"


def _fleet(workspace: Workspace, manifest: Manifest, log_dir: str) -> Fleet:
    """What every worker this turn spawns will share."""
    definition = _definition(workspace, manifest)
    if not definition.exists():
        raise TendError(
            f"the definition {definition} no longer exists, so no worker can run "
            f"it — restore it, or run `steward launch` against its replacement"
        )
    return Fleet(
        definition=definition,
        type=manifest.source.type,
        log_dir=log_dir,
        eval_set_id=resolve_eval_set_id(log_dir, manifest.eval_set_id),
        workers_dir=workspace.workers,
        inflight=workspace.inflight,
        # the workspace rather than wherever a timer happened to fire from, so
        # a definition's relative paths resolve the same way every turn
        cwd=workspace.root,
        args=manifest.source.args or None,
        overrides=worker_overrides(manifest),
        # from the manifest, never this turn's directives: the merge was
        # settled and verified at launch (`_scan.bracket`)
        scanners=manifest.scan.injected if manifest.scan is not None else None,
    )


def _manifest(workspace: Workspace) -> Manifest:
    """Desired state, or an explanation of why there is none."""
    try:
        return read_manifest(workspace.manifest)
    except FileNotFoundError as ex:
        raise TendError(
            "this workspace has no committed manifest, so there is nothing to "
            "converge toward — run `steward launch` to capture the definition "
            "and commit it as desired state"
        ) from ex
    except OSError as ex:
        raise TendError(f"{workspace.manifest} could not be read: {ex}") from ex


def _settings(
    workspace: Workspace,
    history: _History,
    *,
    max_workers: int | None,
    stall_after: int | None,
    samples_ramp: tuple[int, int] | bool | None,
    execute: bool,
    stuck_after: int | None = None,
    preauthorized: dict[str, str] | bool | None = None,
    sync: str | bool | None = None,
    notification: str | bool | None = None,
    scan_model: str | bool | None = None,
) -> _Settings:
    """What to operate under, degrading to the last known good where it must.

    A human may edit `_steward.yaml` at 10pm with a fleet up, and a typo in it must not stop the fleet converging — that is exactly the unattended failure the timer exists to prevent. So a file that will not parse falls back to the settings the last turn recorded, and says so loudly enough that nobody mistakes the run for one following the file.

    **Falling back needs somewhere to fall back to.** With no `observation` in the journal there is no last known good, and running on Steward's own defaults would silently discard whatever the operator wrote — the one outcome worse than stopping. So the first turn after a bad edit refuses, and every turn after a good one degrades.

    **Inspect's words come from the environment here, not from a flag.** `max_tasks` and `max_samples` are `eval_set()`'s and Steward has no spelling of its own for either, so a turn learns them the way an `inspect eval` in the same shell would (`_workspace.overrides`). The run-wide values a launch resolved are not read here at all: they are in the committed manifest, and `resolve_max_tasks` consults them below the environment where the precedence puts them.

    **A `STEWARD_MAX_SAMPLES` outlives its turn**, which `_pin` argues. It is also read in the same breath as the file, so a value neither can parse degrades this turn rather than stopping the fleet.
    """
    # read separately, because they fail separately and only one of them is a
    # file. A bad `INSPECT_EVAL_*` used to be caught alongside a bad
    # `_steward.yaml` and reported as one condition, which lost the file's
    # standing rules -- rules that had parsed perfectly and that an agent is
    # told to get from `steward status`
    directives: Directives | None = None
    try:
        directives = read_directives(workspace.directives)
        overrides = read_overrides(os.environ)
    except DirectivesError as ex:
        if (last := history.pool) is None:
            raise
        pool = Pool(
            max_workers=max_workers if max_workers is not None else last.max_workers,
            max_tasks=last.max_tasks,
            max_samples=last.max_samples,
            samples_ramp=(
                samples_ramp
                if isinstance(samples_ramp, tuple) or samples_ramp is False
                else last.samples_ramp
            ),
            stall_after=stall_after if stall_after is not None else last.stall_after,
        )
        if execute:
            steward_log(
                workspace.log,
                f"{workspace.directives.name} could not be read ({ex}); "
                f"running on the settings the last turn recorded",
            )
        # no interval at all: the file that would have expressed one is the
        # thing that will not parse, and there is no last known good to fall
        # back to since an `observation` does not carry it. Reporting timer
        # drift here would be a second complaint about a file the `degraded`
        # item has already reported
        return _Settings(
            pool=pool,
            degraded=str(ex),
            # the file's mtime only where the file is what failed. Keyed on it
            # regardless, two different broken variables were one item, and
            # acknowledging the first silenced the second -- so an environment
            # failure is keyed on what it said instead
            degraded_at=(
                _stamp(workspace.directives)
                if directives is None
                else _fingerprint(str(ex))
            ),
            # a file that parsed still has rules, and they are still in force:
            # the turn degrades on the *pool*, not on the standing rules, and
            # an agent reading `status` needs the ones it can have
            policies=_policies(directives) if directives is not None else [],
            interval=directives.tend_interval if directives is not None else None,
            # and the store on the same rule the policies keep: a file that
            # parsed still says where it is. A file that did not says nothing,
            # and the readiness item then invites no decision -- which is the
            # safe direction, since the decision it invites is publication
            log_store=resolve_log_store(directives) if directives is not None else None,
            # the reporting threshold degrades to the last known good like the
            # pool; the two authorities degrade to *nothing* -- a standing
            # authorization whose text cannot be read must not be exercised
            stuck_after=(
                stuck_after
                if stuck_after is not None
                else (
                    directives.stuck_after
                    if directives is not None
                    else history.stuck_after
                )
            ),
            stuck_cancel=directives.stuck_cancel if directives is not None else None,
            preauthorized=(
                _granted(preauthorized)
                if preauthorized is not None
                else (
                    _granted(directives.preauthorized)
                    if directives is not None
                    else None
                )
            ),
            # for the same reason as the policies: a file that parsed still
            # says where the workspace goes, and a remote reader watching a
            # degraded run is exactly who needs the file to keep arriving
            sync=sync
            if sync is not None
            else (directives.sync if directives is not None else None),
            # and the channel most of all, since a file that will not parse is
            # one of the things worth being told about. Where the *file* is what
            # failed the variable is read on its own, so the one spelling that
            # could not have been damaged by the edit still answers
            notification=(
                notification
                if notification is not None
                else (
                    directives.notification
                    if directives is not None
                    else declared_notification(os.environ)
                )
            ),
            channel=(
                directives.notification
                if directives is not None
                else declared_notification(os.environ)
            ),
            # the same standing as the channel: where the file is what failed,
            # the spelling the edit could not have damaged still answers
            scan_model=(
                scan_model
                if scan_model is not None
                else (
                    directives.scan_model
                    if directives is not None
                    else declared_scan_model(os.environ)
                )
            ),
        )

    return _Settings(
        pool=resolve_pool(
            directives,
            max_workers=max_workers,
            max_tasks=overrides.max_tasks if overrides else None,
            max_samples=_pin(
                overrides.max_samples if overrides else None,
                directives,
                history,
                samples_ramp,
            ),
            stall_after=stall_after,
            samples_ramp=samples_ramp,
        ),
        degraded=None,
        interval=directives.tend_interval,
        policies=_policies(directives),
        log_store=resolve_log_store(directives),
        stuck_after=stuck_after if stuck_after is not None else directives.stuck_after,
        stuck_cancel=directives.stuck_cancel,
        preauthorized=(
            _granted(preauthorized)
            if preauthorized is not None
            else _granted(directives.preauthorized)
        ),
        sync=sync if sync is not None else directives.sync,
        notification=notification
        if notification is not None
        else directives.notification,
        channel=directives.notification,
        scan_model=scan_model if scan_model is not None else directives.scan_model,
    )


def _policies(directives: Directives) -> list[str]:
    """The standing rules as a list, however they were written.

    One entry for a block of prose and one per item for a list, because those are the two shapes the key accepts and neither should be reshaped into the other. Splitting a paragraph on blank lines would be a guess about where one rule ends, which is exactly the interpreting this layer does not do.
    """
    if directives.policies is None:
        return []
    if isinstance(directives.policies, str):
        return [directives.policies]
    return list(directives.policies)


def _pin(
    given: int | None,
    directives: Directives,
    history: _History,
    samples_ramp: tuple[int, int] | bool | None = None,
) -> int | None:
    """The sample-concurrency pin in force, which may have been set on an earlier turn.

    `--max-workers` and `max_tasks` are settings for one turn and leave no residue: each turn recomputes the fleet from scratch, so a value that lapses simply stops applying. `max_samples` is not like that. It decides a *regime* rather than a quantity — a value pins the setpoint and switches the ramp off entirely (`resolve_samples_ramp`) — and that regime persists in the workers it spawned. A pin that lapsed after one turn would leave the next tend reading a level nobody was ramping, climbing it, and spawning the queue at the ramp's floor instead: the operator's number overridden twice, by a default they never chose, while nobody was watching.

    So the pin is recorded like everything else a turn ran under, and read back here. That matters precisely because the source is a variable: an operator's export reaches the tend they typed and not the 02:00 one, where a launch's own override is durable in the manifest and needs no help. **The way out is a `samples_ramp` *range***, however it is spelled — in the file, in `STEWARD_SAMPLES_RAMP`, or on the command line. All three say *ramp this run*, which is the one instruction that could mean nothing else. `false` does not release it in any spelling: that agrees with the pin rather than contradicting it, and would only substitute Steward's floor for the operator's number.

    Args:
        given: What this shell's `STEWARD_MAX_SAMPLES` or `INSPECT_EVAL_MAX_SAMPLES` said, or `None`. Not what the *run* said: a launch's own value is in the committed manifest, which every turn reads anyway, so there is nothing about it for the journal to remember.
        directives: The parsed `_steward.yaml` and environment, for the release.
        history: The journal, for what an earlier turn recorded.
        samples_ramp: What this invocation's `--samples-ramp` said, or `None`. Outranks the file for the release, the same way it outranks it everywhere else.

    Returns:
        The pinned setpoint, or `None` to leave the chain to the definition and the ramp.
    """
    if given is not None:
        return given
    ramp = samples_ramp if samples_ramp is not None else directives.samples_ramp
    if isinstance(ramp, tuple):
        return None
    return history.pool.max_samples if history.pool is not None else None


def _stamp(path: Path) -> str | None:
    """A file's modification time, as an item id can carry it.

    Nanoseconds rather than seconds: someone fixing a typo and saving twice inside one second is exactly the person this must not go quiet on.
    """
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return None


def _fingerprint(message: str) -> str:
    """An identity for a failure that has no file to be stamped from.

    Short and content-derived: two different broken variables must key two different items, and the *same* broken variable across turns must key one — which is exactly what a hash of the refusal gives, where a timestamp would give a new item every ten minutes and a constant would give one item forever.
    """
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]


def _positive(value: Any) -> int | None:
    """A positive integer from a journal payload, which may hold anything."""
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _record(
    workspace: Workspace,
    result: TendResult,
    *,
    pool: Pool,
    tuning: dict[str, Any],
    stuck_after: int | None = None,
) -> None:
    """Append this turn's observation to the journal.

    After the actions rather than before, so it records what happened rather than what was intended. A turn interrupted between the two loses its observation and repeats no work: the next turn re-reads the same directory and reaches the same place.
    """
    summary = result.summary
    ramp = pool.samples_ramp
    append_event(
        workspace.journal,
        OBSERVATION,
        tasks=summary.tasks,
        states=summary.states,
        reasons=summary.reasons,
        running=summary.running,
        spawned=len(result.spawned),
        reaped=len(result.reaped),
        archived=len(result.archived),
        queued=summary.queued,
        stalled=summary.stalled,
        orphans_running=summary.orphans_running,
        unreadable=summary.unreadable,
        drift=result.drift,
        degraded=result.degraded,
        failures=result.failures,
        verdict=result.verdict.value,
        # the ids rather than the items: this is what the *next* turn diffs
        # against, and a rendered summary is not something to diff
        items=[item.id for item in result.items],
        # open windows by class, for the time series an agent reads: whether a
        # population is growing is a diff over these, not a re-derivation
        anomalies={
            anomaly.class_key: anomaly.evidence.count
            for anomaly in result.anomalies.open
        },
        # the same, for tasks. `states` counts them, and a count cannot tell one
        # task finishing while another is reset apart from nothing happening.
        # A held task is left out so its finish stays unspent — the other half
        # of the hold, and the half without which it is a suppression
        complete=[
            identifier
            for identifier in _finished(result.progress)
            if identifier not in result.held
        ],
        # what this turn ran under, which is what a later turn reads back when
        # `_steward.yaml` will not parse
        settings={
            "max_workers": pool.max_workers,
            "max_tasks": pool.max_tasks,
            "max_samples": pool.max_samples,
            "samples_ramp": list(ramp) if isinstance(ramp, tuple) else ramp,
            "stall_after": pool.stall_after,
            # beside the pool's keys because it degrades with them; `_pool`'s
            # payload sentinel keys on `stall_after` alone, so an extra key
            # costs it nothing
            "stuck_after": stuck_after,
        },
        # what the next turn's window measures against (`_tend.tuning.Baseline`)
        tuning=tuning,
    )


def _write_status(
    workspace: Workspace, result: TendResult, *, failing_since: str | None
) -> None:
    """Rewrite `status.md`, atomically, and never at the cost of the turn.

    Written through a temporary file and renamed, because this is the file a remote reader watches and half of it would read as a run in a state it was never in. A failure to write it is machinery: the turn already happened, and the journal already recorded it — but it is also the failure a remote reader detects *only* by this file going stale, which can take hours to notice. So the edges are journalled (`_mark`) and the episode becomes an item (`items.STATUS_UNWRITABLE`).

    Args:
        workspace: The workspace whose snapshot this is.
        result: The turn to render.
        failing_since: The open episode's start, or `None` where the last write worked — what makes a failure the *first* one worth recording and a success a recovery.
    """
    if _write_rendered(
        workspace,
        workspace.status,
        status_markdown(result),
        failing_since=failing_since,
    ):
        result.rendered.append(workspace.status.name)


def _write_anomalies(workspace: Workspace, result: TendResult) -> None:
    """Rewrite `anomalies.md` beside the status, on the same terms.

    **Mirrored every turn rather than written at signoff**, because workflow.md §14 makes signoff the moment the caveat list stops changing rather than the moment it exists: a list that appeared only at the end is a list nobody read while there was still time to disagree with it, and one of the two ways into it is an acknowledgment somebody typed at 3am.

    The caveats travel on the result rather than being recomputed here, because `status.md` renders the same list one line each and a second computation is a second chance to disagree. What does *not* travel is the census they were computed from — the largest thing a turn holds, in a value whose whole job is to be small enough to print.

    **It takes no part in the failure episode, and `status.md` alone does.** Both files fail together for every cause anybody has — a full disk takes both, a read-only mount takes both — but they were sharing one *unkeyed* episode, so this file succeeding while the status did not recorded a **restoration** and closed an episode that was still open. The item then vanished and returned every turn, which is a persistent failure rendered as a flapping one. The episode exists because a remote reader detects a dead timer by `status.md` going stale (`items.STATUS_UNWRITABLE`); this file going unwritable alone is worth a line in `steward.log` and not a second item saying the same thing about the same directory.
    """
    if _write_rendered(
        workspace,
        workspace.anomalies,
        anomalies_markdown(result.caveats, scored=marks_note(result) or ""),
        failing_since=None,
        episode=False,
    ):
        result.rendered.append(workspace.anomalies.name)


def _write_analysis(
    workspace: Workspace, result: TendResult, sections: Sequence[Section]
) -> None:
    """Write `analysis.md` back with this turn's facts folded in.

    **The only generated document that is not regenerated.** What is written is the file as it stood with the facts blocks replaced, so an `OSError` here costs one turn's freshness on a facts list and can lose nothing that was written by hand — the merge is pure and the write is atomic, so the previous file survives whole.

    **The file is re-read here rather than reused from the top of the turn**, and the difference is somebody's work. The facts were composed before the turn acted, because the items had to report what is unwritten; everything between then and now is spawns, requeues, invalidations and archive moves, which on a busy turn is minutes. The other author is a person or an agent with the file open, and this write is an atomic replace — so merging a snapshot taken before all of that would overwrite whatever they saved in the meantime. Re-reading narrows the window to the microseconds between this read and the `replace` below, which is the same exposure `status.md` has always had and the smallest one available without taking a lock on a markdown file.

    Out of the failure episode for `anomalies.md`'s reason exactly: the episode exists because a remote reader detects a dead timer by `status.md` going stale, and a second item saying the same thing about the same directory is noise.

    A section whose markers did not pair is reported here rather than fixed. Its text came back byte-identical — the merge declined to guess at a boundary in somebody's work — and the repair is a person putting the marker back.

    A file that exists and would not read (`_read_authored`) is the one case where nothing at all happens: writing what a merge from nothing would have produced would replace an investigation with a stub. An **empty body** is the other silence and a benign one — no file yet, and no task has landed anything to explain.
    """
    authored = _read_authored(workspace.analysis)
    if authored is None:
        steward_log(
            workspace.log,
            f"{workspace.analysis.name} could not be read and was left untouched",
        )
        return
    analysis = merge_analysis(authored, sections)
    if not analysis.body:
        return
    for identifier in analysis.damaged:
        steward_log(
            workspace.log,
            f"the analysis.md section for {identifier} has unpaired "
            f"`steward:begin`/`steward:end` markers and was left untouched",
        )
    if _write_rendered(
        workspace,
        workspace.analysis,
        analysis.body,
        failing_since=None,
        episode=False,
    ):
        result.rendered.append(workspace.analysis.name)


def _read_authored(path: Path) -> str | None:
    """One co-authored file as it stands: its text, `""` where there is none yet, or `None` where it exists and will not read.

    **The three answers are three different acts, and collapsing two of them destroys somebody's work.** An absent file is composed from scratch and written. A file that reads is merged into and written back. A file that *exists* and will not read is left completely alone — because the merge would compose a fresh document from nothing and the write is an atomic replace, so treating a permissions error or a transient read failure as *empty* would overwrite an investigation with a stub. This is the one generated document whose contents cannot be regenerated, which is exactly why it declines rather than guesses.

    **`newline=""` rather than the default**, because the default is universal-newline mode and the merge downstream promises to return the authored bytes unchanged. Reading a CRLF file with translation on hands the merge a document that already has LF endings, and the atomic replace then writes the whole file back in the endings Steward preferred — churn across every line of somebody's prose, from a turn that changed one bullet.

    **Not decoding is a read failure, not a crash.** `UnicodeDecodeError` is a `ValueError` and so escapes an `OSError` handler entirely: a file saved in latin-1 would take down every `status` and every `tend` on the workspace, on the strength of a document that nothing else in the turn depends on. It is the same answer as a permissions error — *this exists and I cannot read it* — and gets the same one.
    """
    try:
        with path.open(encoding="utf-8", newline="") as file:
            return file.read()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        return None


def _write_rendered(
    workspace: Workspace,
    path: Path,
    body: str,
    *,
    failing_since: str | None,
    episode: bool = True,
) -> bool:
    """Rewrite one generated document, atomically, and never at the cost of the turn.

    One writer for both files because they fail for the same reason at the same moment — a full disk takes both, a read-only mount takes both — and a second episode mechanism would be a second item saying the same thing about the same directory.

    **`newline=""` so the body is written as it was composed.** The default translates every line ending in the body to `os.linesep`, which on Windows would turn `analysis.md`'s byte-preserving merge into a whole-file rewrite the moment a turn ran there — and the two regenerated documents have no reason to want the platform's endings either.

    Returns:
        Whether the document was written. A failure here never fails the turn — the turn already happened and the journal already recorded it — so the answer is the only thing that distinguishes *written* from *left as it was*, and `signoff` needs that distinction because nothing tends the run afterwards.
    """
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(body, encoding="utf-8", newline="")
        temporary.replace(path)
    except OSError as ex:
        # `missing_ok` covers the temporary never having been created; it does
        # not cover the unlink itself failing, and a cleanup that raises out of
        # a handler would fail a turn that already happened
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        steward_log(workspace.log, f"could not write {path.name}: {ex}")
        if episode and failing_since is None:
            _mark(workspace, "status_unwritable")
        return False
    if episode and failing_since is not None:
        _mark(workspace, "status_unwritable_restored")
    return True


def _mark(workspace: Workspace, action: str, **fields: Any) -> None:
    """Journal an episode edge, and never at the cost of the turn.

    Swallowing `OSError` here is deliberate and honest about its gap: the failures these edges record are write failures, and the case where the whole disk is full can record nothing at all (workflow.md §9.2). What survives that case is the failure's own `steward.log` line and, for `status.md`, the staleness a remote reader was always going to see.
    """
    try:
        append_event(workspace.journal, ACTION, action=action, **fields)
    except OSError:
        return


def _sync(
    workspace: Workspace, settings: _Settings, log_dir: str, *, failing: dict[str, str]
) -> None:
    """Propagate the workspace to the log directory, if it propagates anywhere.

    **Last, and after `status.md` is written**, which is what makes it cheap to be interrupted by: everything this turn did has already happened and already been recorded, so a propagation that runs long or not at all costs a remote reader ten minutes of freshness and costs the run nothing.

    Also the reason a slow one needs no cancellation machinery. A turn holds the claim while it runs, so an overrunning propagation means the next timer fire is refused — the ordinary path rather than a problem — and the interval after it converges.

    Truncating `steward.log` happens here rather than beside the writer: the writer is the thing that must not fail, and this is the last point in the turn where the file is finished being written to.

    The report is consumed rather than dropped: a propagation that failed is a remote reader losing their only channel, which `sync_workspace` says once in `steward.log` and then — because that file is truncated and the failure repeats every turn — this records as an episode (`_mark`), keyed by destination, for `items.SYNC_FAILED` to gate on.

    An episode whose destination is no longer the one being synced — the target changed, or sync was turned off — closes here too: nothing will ever write its `sync_restored` otherwise, and a permanent warning about a destination nobody configured anymore is noise, not news.
    """
    truncate_log(workspace.log)
    target = sync_target(settings.sync, log_dir)
    for stale in failing:
        if stale != target:
            _mark(workspace, "sync_restored", target=stale)
    if target is None:
        return
    report = sync_workspace(workspace, target)
    if report.failures:
        if target not in failing:
            _mark(workspace, "sync_failed", target=target)
    elif target in failing:
        _mark(workspace, "sync_restored", target=target)


def _live(
    inflight: InFlight,
    logs: ObservedLogs,
    *,
    stuck_after: float = DEFAULT_STUCK_AFTER,
) -> LiveFleet:
    """Ask the running workers how they are getting on.

    **Only the ones that are running, and only when some are.** The in-flight record already answers *is anything alive* for free, so a finished campaign — the common shape late on — pays nothing at all for the live columns. A worker that has not yet bound its control socket has no entry here either; it is in the window before its `eval_set()` boundary, where there is genuinely nothing to ask.

    The observation comes along because a packed worker reports a row per task and names each one only by the log it is writing. That mapping is a by-product of a read this turn has already done, so correlation costs nothing beyond the dictionary; without it every row of a packed worker is unnameable, and each of its tasks reads `finished` while it is still running.
    """
    targets = [
        LiveTarget(identifiers=worker.identifiers, pid=worker.pid, socket=worker.socket)
        for worker in inflight.running
        if worker.socket is not None
    ]
    return read_fleet(targets, _locations(logs), stuck_after=stuck_after)


def _findings(
    workspace: Workspace,
    manifest: Manifest,
    observed: ObservedTasks,
    logs: ObservedLogs,
    inflight: InFlight,
    log_dir: str,
    scan_id: str | None,
    *,
    execute: bool,
    fold_failing: str | None,
) -> ScanFindings:
    """Fold the workers' buffered scan rows, then read what they flagged.

    The tend's half of the scan bracket (`_scan.summary`), and the second half of it is the reason the first has to happen here: workers in selection mode never enter upstream's `scan_context`, so **nothing but this fold ever compacts a row**. Without it the whole census is blind to scanning until signoff.

    **The fold is an executing turn's, the read is both dispositions'.** A `status` previews the anomalies it would find and mutates nothing, which is the contract every other part of it honours — so it reads whatever the last tend folded and is at worst one interval behind.

    **Folded while the run is not quiescent, or while a fold is owed.** The first is `running or departed`: a worker writes rows as its samples settle, and one that has left but not yet been reaped is the case where the last of them landed after the previous fold. Once the reap lands there is nothing new and a settled campaign pays nothing per turn — which matters, because a mid-run fold re-compacts the whole buffer and its cost grows with the run.

    **The second is an open episode, and without it the cheap gate has a hole that ends at the signature.** A fold that failed on the departure turn is a fold that never happens: the reap lands, the gate stops firing, and rows sit in the buffer through every later tend — until signoff's terminal finalize folds them, *after* the gate has passed, revealing a finding the signature does not cover. So a failure opens an episode (`scan_fold_failed`) that keeps the fold running every turn until one succeeds, on `status.md`'s and the propagation's mechanics exactly.

    Never raises. A directory that will not fold costs this turn's freshness and is retried; one that will not *read* is a different thing entirely and is reported (`ScanFindings.unreadable`), because unread and unflagged are not the same answer.

    **A configured scan with no id is that same distinction one step earlier, and it read as *no scanning at all*.** The two conditions were one predicate: no `material` means this run scans nothing, and an empty census is simply true for it. A missing `scan_id` — `.eval-set-id` gone from the log directory and no id in the manifest — means the scan is configured and its directory cannot be located, so nothing can be folded, nothing read, and the census, the coverage column and the terminal finalize all quietly become *there was never anything here*. A signature then says nothing was flagged about transcripts nobody could look for. It is reported as unreadable, which is the vocabulary this file already has for evidence that exists and cannot be sized, and which the signoff gate already refuses on.
    """
    material = manifest.scan
    if material is None:
        return ScanFindings()
    if scan_id is None:
        return ScanFindings(
            unreadable=[
                UnreadableLog(
                    location=log_dir,
                    reason=(
                        "this run scans, and neither the log directory's "
                        "`.eval-set-id` nor the committed manifest says which "
                        "scan the rows belong to"
                    ),
                    what="this run's scan results",
                )
            ]
        )
    scanners = tuple(sorted(merged_scanners(material)))
    if not scanners:
        return ScanFindings()
    scan_dir = scan_dir_location(log_dir=log_dir, scan_id=scan_id, scans=material.scans)
    if execute and (inflight.running or inflight.departed or fold_failing):
        started = time.monotonic()
        try:
            sync_scan(log_dir=log_dir, scan_id=scan_id, scans=material.scans)
        except Exception as ex:
            steward_log(
                workspace.log,
                f"the scan rows in {scan_dir} could not be folded: "
                f"{type(ex).__name__}: {ex}",
            )
            if fold_failing is None:
                _mark(workspace, SCAN_FOLD_FAILED, target=scan_dir)
        else:
            # the cost this fold pays is the one step 29 asked to be *measured*
            # before it is engineered around: it re-compacts the whole buffer,
            # so it grows with the run rather than with the turn
            steward_log(
                workspace.log,
                f"folded the scan rows in {time.monotonic() - started:.1f}s",
            )
            if fold_failing is not None:
                _mark(workspace, SCAN_FOLD_RESTORED, target=scan_dir)
    try:
        return scan_findings(
            scan_dir, scanners=scanners, attempts=scan_attempts(observed, logs)
        )
    except Exception as ex:
        # the whole directory rather than one scanner's file — a `_scan.json`
        # that will not open, a store that will not answer. Reported for the
        # reason a single parquet is: the run would otherwise be signed on the
        # assumption that nothing was flagged
        if execute:
            steward_log(
                workspace.log,
                f"the scan rows in {scan_dir} could not be read: "
                f"{type(ex).__name__}: {ex}",
            )
        return ScanFindings(
            unreadable=[
                UnreadableLog(
                    location=scan_dir,
                    reason=f"{type(ex).__name__}: {ex}",
                    what="this run's scan results",
                )
            ]
        )


def _current_locations(observed: ObservedTasks) -> dict[str, str]:
    """Task identifier to its current attempt's location, for the dispositions fold."""
    return {
        task.identifier: task.current.location
        for task in observed.tasks
        if task.current is not None
    }


def reused_samples(
    observed: ObservedTasks, found: ScanFindings
) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    """Per resumed task with scan rows, the sample uuids its current log actually holds — and the ones that would not read.

    **The one read this turn pays that no other part of it needs**, and it is gated twice so it stays a handful of logs rather than a directory. A task with **no superseded attempt** is skipped: its rows and its log name the same file, so the location test is already exact and a summaries read would buy nothing. A task with **no scan rows** is skipped for the same reason from the other side — nothing scan-shaped exists to be stranded, and its coverage is zero however it is computed.

    What is left is the shape the hazard actually lives in: a task that was retried, whose new log carries the samples that already succeeded under their original uuids, whose scan rows still name the file the scanner read them from. Both readers of this take it — coverage's numerator and `in_results`' narrowing — because a count and the list it summarizes computed from two reads is how the two come to disagree in print.

    **A log that would not read is returned separately, because the two readers owe it opposite answers.** For the narrowing, absent is right: fall back to the location test, which loses nothing that was already found. For coverage it is the one thing that must not be treated as absent — absent means *this task was never resumed, so counting the rows is exact*, and here the rows are the union across attempts and counting them can report a run as fully scanned over samples it replaced.

    Returns:
        The uuid sets that read, and the identifiers of the resumed tasks whose current log did not.
    """
    reused: dict[str, frozenset[str]] = {}
    unverified: set[str] = set()
    for task in observed.tasks:
        if task.current is None or not task.superseded:
            continue
        if task.identifier not in found.recorded:
            continue
        if (uuids := sample_uuids(task.current.location)) is not None:
            reused[task.identifier] = uuids
        else:
            unverified.add(task.identifier)
    return reused, frozenset(unverified)


def _cleared(observed: ObservedTasks) -> set[str]:
    """The subjects of the two acknowledgeable conditions that have demonstrably stopped being true.

    An acknowledgment names a **condition**, not an instant: *this log will not read*, *this task has stopped making progress*. Both can stop being true afterwards — somebody replaces a truncated upload, a task the guard gave up on is relaunched and finishes — and the caveat then describes a hole the numbers do not have. So `anomalies_md.caveats` drops an acknowledgment once what it acknowledged has cleared, and this is the reading of the directory that answers it (`items.MARKED` names the two kinds).

    **What cleared, rather than what stands**, so a subject this cannot place keeps its caveat. The inverse reads more naturally and fails the wrong way: an acknowledgment recorded against something no longer in the observation at all — a log curated away, an identifier the definition dropped — would vanish from the record silently, and losing a footnote nobody asked to lose is the worse of the two mistakes.
    """
    return {
        attempt.location
        for task in observed.tasks
        for attempt in (task.current, *task.superseded)
        if attempt is not None
    } | {task.identifier for task in observed.tasks if task.state is TaskState.COMPLETE}


def _latched(anomalies: Anomalies, launched: str | None) -> set[str]:
    """The tasks reconcile stops respawning, with the launch that would put them back in play.

    An acceptance ends a task's attempts, and it stays ended — a person decided the results stand without it, and nothing mechanical should overrule that. **What re-arms it is a launch**, because committing a manifest is the one moment desired state is decided: the same doctrine that makes `restore_log` launch-only. So the latch holds while the accepting ruling postdates the most recent launch, and a relaunch that re-asks for the task simply outdates it — no new record, one comparison, and the same shape as the stall guard's own forgiveness instant.

    **Each task is compared against the decision that accepted *it*.** An earlier version asked whether any settled window postdated the launch and then latched every accepted identifier, which is a different question with the same answer most of the time: after a relaunch, an unrelated `exclude` on the same task was enough to re-latch it and stop its respawns under a decision nobody had made about whether it should run.
    """
    settled = accepted_tasks(anomalies)
    if launched is None:
        return set(settled)
    return {
        identifier
        for identifier, accepted_ts in settled.items()
        if is_after(accepted_ts, launched)
    }


def _granted(value: dict[str, str] | bool | None) -> dict[str, str] | None:
    """The standing grants actually in force: a mapping grants; `false` and unset grant nothing.

    `False` exists so a narrower scope can *decline* the file's grants rather than merely not add to them — `--preauthorized false` on a turn that must not auto-rule — where `None` defers to the file.
    """
    return value if isinstance(value, dict) and value else None


def _cancel_authority(
    stuck_cancel: bool | list[str] | None,
) -> bool | tuple[str, ...] | None:
    """The `stuck_cancel` grant, normalized for the item routing: `True`, a tuple of function names, or `None` for nothing."""
    if stuck_cancel is True:
        return True
    if isinstance(stuck_cancel, list):
        return tuple(stuck_cancel)
    return None


def _locations(logs: ObservedLogs) -> dict[str, str]:
    """Log location to task identifier, for naming a packed worker's rows.

    Every attempt rather than the current one: a worker resuming a task writes to the log it was handed, which is the newest attempt but not necessarily the one `current` elects — that rule prefers the latest *success*, and a task being resumed has none.
    """
    return {
        attempt.location: identifier
        for identifier, attempts in logs.attempts.items()
        for attempt in attempts
    }


def _definition(workspace: Workspace, manifest: Manifest) -> Path:
    """Where the manifest's definition is, anchored to the workspace when relative."""
    path = Path(manifest.source.path)
    return path if path.is_absolute() else workspace.root / path


@dataclass(frozen=True)
class _Drift:
    """Whether the definition changed, and what it is now."""

    changed: bool
    digest: str | None


def _drifted(workspace: Workspace, manifest: Manifest) -> _Drift:
    """Whether the definition has changed since it was captured.

    One hash of one file, cheap enough for every turn, and the guard against the failure that actually costs a night: an edit made at 11pm that nobody applied, converging all night toward the manifest captured before it. Never acted on here — `launch` is the only verb that reads a definition.

    The digest comes back with the answer because it is what keys the item: acknowledging a deliberate edit must not also acknowledge the next one, and the hash is precisely the thing that distinguishes them.
    """
    try:
        digest = definition_hash(_definition(workspace, manifest))
    except OSError:
        # gone, or unreadable: either way it is not the file that was captured,
        # which is the same thing drift means
        return _Drift(changed=True, digest=None)
    return _Drift(changed=digest != manifest.source.content_hash, digest=digest)


def _log_dir(workspace: Workspace, manifest: Manifest) -> str:
    """The run's log directory, as the launch that committed this manifest resolved it.

    **Read back, never re-derived.** The resolution takes a `log_root` that arrives in the environment, and a scheduled tend inherits almost none of one — so a turn that resolved this for itself would read `logs/` at 02:00 while the fleet wrote to the root, and every task would land and then read as never started (`_timer.env`, *AMBIENT*).

    A manifest committed before the field existed carries none, and resolving it without a root reproduces exactly the answer it was committed under.
    """
    return manifest.log_dir or resolve_log_dir(workspace, manifest)
