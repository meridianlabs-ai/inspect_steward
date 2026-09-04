"""`launch` — the one verb that reads the definition, and therefore the only one that decides.

Everything else in Steward converges toward a manifest somebody else committed. This is where the manifest comes from, which makes it a composition rather than a component: capture, gate, commit, restore, stop, arm, record, tend. Nothing here is new machinery; what is new is the **order**, and four of the orderings have a wrong-looking alternative worth stating.

**The claim is taken before the capture, and held across all of it.** Capture is minutes for a Hawk config, which is a long time to hold a lock whose whole design rests on being short-lived (`_workspace.claim`). The alternative is worse in a way that costs results rather than time: two launches interleaving one's capture with the other's commit, or — the real hazard — a scheduled tend firing mid-launch and spawning workers for tasks the commit is about to orphan. A tend that fires meanwhile is refused, which is the ordinary path rather than a problem: *a tend is built to be interrupted*, and the next interval converges.

**The gate sits between the capture and the commit, and there is nowhere else it could sit.** `reconcile` archives orphans unconditionally, by design — once desired state says a task is not in the eval set, moving its logs is bookkeeping. So the instant a manifest that orphans tasks is written, the 02:00 tend archives them with nobody present. Consent is taken here or it is not taken.

**Arming happens before the first tend**, so a launch whose own turn fails still leaves the run supervised and the next interval picks it up. The timer firing during the launch's own tend is a non-event — it meets the held claim and does nothing.

**The `launched` event is appended before the tend**, so that turn's item projection already sees it. Otherwise a `--no-timer` launch's own first `status.md` would report the run as supervised, which is the one thing execution.md §8.3 asks this event to prevent.

**The restore happens after the commit**, because what a restore is *for* is the identifiers the new manifest asks for. Before the commit there is nothing authoritative to restore against.

**Two correctness traps are written into the code below rather than left to a reader.** The definition path is recorded in the manifest verbatim and resolved by every later tend against the workspace root, so a path relative to the shell's cwd would send every subsequent turn looking somewhere else. And capture's cwd must equal the fleet's, because the manifest records no cwd — a launch typed from a subdirectory would enumerate under one and execute under another, and nothing downstream could detect it.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from inspect_ai._eval.eval_set_overrides import (
    INSPECT_EVAL_SET_OVERRIDES,
    EvalSetOverrides,
)
from inspect_ai._util.file import basename, filesystem

from .._evalset.archive import archive_dir, restore_log
from .._evalset.detect import DefinitionType
from .._evalset.manifest import (
    Manifest,
    ManifestError,
    ManifestTask,
    manifest_digest,
    read_manifest,
    write_manifest,
)
from .._evalset.observe import (
    ObservedLogs,
    incomplete_reason,
    observe_logs,
    read_attempt,
)
from .._evalset.read import ReadEvalSetError, read_eval_set
from .._scan import (
    ScanError,
    initialize_scan,
    merged_scanners,
    scan_digest,
    scan_material,
    verify_scan,
)
from .._scan.model import establish_scan_model
from .._schedule import RunningWorker
from .._store import StoreError, copy_log, open_store, store_location
from .._tend import TendResult, tend
from .._timer import (
    Armament,
    TimerError,
    arm,
    disarm,
    explain_env,
    resolved_env,
    unavailable_credentials,
)
from .._worker import (
    Stop,
    StopRequest,
    resolve_eval_set_id,
    resolve_inflight,
    stop_workers,
)
from .._workspace import (
    ACTION,
    LAUNCHED,
    LOG_DIR,
    Claim,
    DirectivesError,
    Held,
    JournalRead,
    Workspace,
    acquire,
    append_event,
    ensure_gitignore,
    read_directives,
    read_journal,
    read_overrides,
    read_smoked,
    resolve_interval,
    resolve_log_dir,
    resolve_log_root,
    resolve_log_store,
    steward_log,
)
from .delta import Change, Delta, compute_delta
from .pools import POOLS_ADVISED, PoolAdvice, advise


class LaunchError(Exception):
    """A launch could not be completed.

    A message for an operator, never a traceback: everything reachable here is a condition of the machine or the definition rather than a defect. A refusal at the archive gate is *not* one of these — that is an outcome with a delta attached, and the delta is the whole point of it.
    """


@dataclass(frozen=True)
class Reuse:
    """One task this launch does not have to run, and where its result came from."""

    identifier: str
    key: str
    """Display key, so the delta names a task the way every other row does."""

    source: str
    """Where the store had it. **Recorded rather than counted**, because an identifier match is a strong claim and not an unlimited one: it guarantees the task, args, model, solver, resolved plan, generate config and execution limits are identical, and guarantees nothing about package versions or a dataset loaded from a mutable source. Whether that is good enough is the reader's judgement, and they cannot make it without knowing whose log this is."""

    location: str
    """Where it now is, inside this run's log directory."""


@dataclass(frozen=True)
class Launch:
    """What a launch did, or what it stopped short of doing."""

    manifest: Manifest
    """The manifest just captured, committed or not."""

    delta: Delta
    """What committing it would change."""

    committed: bool = False
    """Whether the manifest became desired state. `False` only ever means the archive gate refused — every other failure raises, because every other failure leaves nothing to report."""

    armed: Armament | None = None
    """The timer installed, or `None` where the launch was told not to arm one."""

    disarmed: str | None = None
    """The scheduler a `--no-timer` launch *removed*, or `None` where there was nothing to remove. Skipping the arming is not the same act as ending the supervision, and on a re-launch only the second one is what was asked for."""

    scan_dir: str | None = None
    """The scan directory this launch initialized, or `None` where the launch stopped at the gate. Workers refuse to record without it, which is why laying it down is the launch's job rather than the first turn's discovery."""

    restored: list[str] = field(default_factory=list[str])
    """Logs moved back out of the archive, by their new location."""

    reused: list[Reuse] = field(default_factory=list[Reuse])
    """Tasks satisfied from the log store rather than run — rung 2 of the convergence ladder, between the archive and a worker (execution.md §5.6)."""

    stopped: list[Stop] = field(default_factory=list[Stop])
    """Workers stopped because the manifest no longer names what they were running."""

    turn: TendResult | None = None
    """The first turn, which is what actually spawns the first workers."""

    failures: list[str] = field(default_factory=list[str])
    """Things that did not work but did not stop the launch — a log that would not move, a worker that would not stop. Reported here and in `steward.log`, and left for the next turn, which decides again from what it finds."""

    unrehearsed: str | None = None
    """Why no passing smoke covers this capture, or `None` where one does.

    **A warning, deliberately, and never a refusal.** Whether a smoke still applies is answered by the task identifiers — a definition that changed produced different ones — which gives the check a precise question to ask instead of guessing at which edits matter. What it must not do is refuse: re-launching after a fix and resuming an interrupted run are both legitimate reasons to have no current rehearsal, and a hard gate here would only teach people to route around it (workflow.md §7.1).
    """

    pools: PoolAdvice | None = None
    """A host whose Docker will run out of bridge networks before it runs out of room, or `None` where it will not — no Docker, pools already carved finely enough, or a run small enough that thirty networks is ample (`_launch.pools`).

    **Advice rather than an outcome, and offered once per workspace.** Nothing about the launch depends on it: the run starts either way and the fix needs a daemon restart the launch has no business performing. It rides here so the surface that has an operator in front of it can offer to write the file, and so `--json` carries it for one that does not.
    """


def launch(
    workspace: Workspace,
    definition: Path,
    *,
    args: dict[str, Any] | None = None,
    type: DefinitionType | None = None,
    accept_archive: bool = False,
    timer: bool = True,
    env_check: bool = True,
    log_root: str | bool | None = None,
    log_store: str | bool | None = None,
    overrides: dict[str, Any] | None = None,
    max_workers: int | None = None,
    stall_after: int | None = None,
    samples_ramp: tuple[int, int] | bool | None = None,
    stuck_after: int | None = None,
    preauthorized: dict[str, str] | bool | None = None,
    tend_interval: int | None = None,
    sync: str | bool | None = None,
    notification: str | bool | None = None,
    scan_model: str | bool | None = None,
    break_stale: bool = True,
) -> Launch | Held:
    """Capture a definition, commit it as desired state, arm a timer, and tend once.

    Args:
        workspace: The workspace to launch in.
        definition: The definition to capture. Recorded relative to the workspace root where it lives inside one, so that a workspace which moves still resolves it.
        args: Arguments for the definition. `None` reuses the committed manifest's, which is what keeps a re-launch from capturing a different eval set and proposing to archive the first one. **An empty mapping is not the same thing**: it asks for the definition's own defaults, which is the only way back once a launch has passed arguments.
        type: Explicit definition type. `None` reuses the committed manifest's, then falls back to detection.
        accept_archive: Commit even though tasks would leave `logs/`.
        timer: Arm a timer. `False` launches unsupervised and records that it did.
        env_check: Refuse to arm when a scheduled tend would not inherit this shell's credentials. Checked **before** the capture, so a Hawk config does not spend five minutes resolving packages on the way to a refusal.
        log_root: The root this machine keeps eval logs under, overriding `_steward.yaml`. Used only where the definition names no `log_dir` of its own, in which case the run writes to `<root>/<workspace name>`. `False` keeps this run's logs in the workspace whatever the machine says; `None` defers to the file and the environment.
        log_store: Log store for this run — a path or `auto` — overriding `_steward.yaml`. `False` declines the one the file or the environment configured; `None` defers to them. **Configuring one is the whole opt-in for reading it**: there is nothing to protect against, since a match means the identifier is equal and what it points at was published by a signoff. Reads are still *reported*, which is visibility rather than consent.
        overrides: Inspect's own eval-set arguments for this run, already parsed, keyed as `EvalSetOverrides` spells them. Merged over what `STEWARD_*` and `INSPECT_EVAL_*` say and honoured by the capture, so the manifest describes the run that will happen (`_workspace.overrides`). `None` — nothing typed and nothing exported — reuses the committed manifest's, for the reason `args` does. **An empty mapping is not the same thing**: it asks for the definition's own shape, which is the only way back once a launch has passed one.
        max_workers: Worker processes for the first turn, overriding `_steward.yaml`. `None` expresses no preference and defers to the file, which itself defaults to a process per task — it does not request that width.
        stall_after: Fruitless respawns before a task is given up on, overriding `_steward.yaml`.
        samples_ramp: The ramp's envelope, overriding `_steward.yaml`.
        stuck_after: Seconds of quiet before a running sample is reported stuck, overriding `_steward.yaml`. For this launch's own turn only, like `sync` — a reporting threshold, never a limit.
        preauthorized: Rulings granted in advance — class patterns to dispositions — overriding `_steward.yaml`. `False` declines every standing grant. For this launch's own turn only, like `sync`.
        tend_interval: Seconds between scheduled tends, overriding `_steward.yaml`. Already parsed — the flag is validated at the door.
        sync: Where to propagate the workspace, overriding `_steward.yaml`. `False` propagates nowhere; `None` defers to the file, which itself defaults to the log directory. For this launch's own turn only — every later tend reads the file again, because unlike the overrides this is not a property of the run.
        notification: Where Steward posts, overriding `_steward.yaml`. `False` silences Steward's own posts and never the fleet's, whose notifications are blocking human-in-the-loop moments. For this launch's own turn only, like `sync` — the durable spelling is the file key, which is the one still there at 02:00.
        scan_model: The model scanners use, overriding `_steward.yaml`. `False` configures none, leaving scanners on each sample's own model. For this launch's own turn only, like `notification` and on its pattern — every later tend resolves the file key again (`_scan.model`).
        break_stale: Kill a wedged claim holder and take the claim from it.

    Returns:
        What the launch did, or a `Held` naming the holder that would not give up the claim. A `Launch` with `committed` false is the archive gate refusing, which is an answer rather than a failure.

    Raises:
        LaunchError: The definition could not be read, the credentials check refused, no timer could be armed, or the committed manifest could not be replaced.
        DirectivesError: `_steward.yaml` could not be parsed. Launching against settings nobody can read is the one place degrading would be wrong — a tend degrades because a fleet must keep converging, and a launch has nothing to keep going.
        ManifestError: What is committed is not a manifest. A launch is exactly the command that replaces it, so this reports rather than overwrites: the delta would otherwise be computed against nothing and propose archiving a directory full of real results.
    """
    # read once and resolved twice: two reads could disagree if the file were
    # edited between them, and a launch that armed one interval while recording
    # another would be a run nobody could explain
    directives = read_directives(workspace.directives)
    interval = resolve_interval(directives, tend_interval=tend_interval)
    # resolved to a location here rather than carried as the setting, so the
    # journal, the printed delta and the store itself all name one place. A
    # relative `log_store` is relative to the *workspace*, which does not move,
    # and not to wherever the command happened to be typed
    configured = resolve_log_store(directives, log_store=log_store)
    store = (
        store_location(configured, workspace.root) if configured is not None else None
    )
    root = resolve_log_root(directives, log_root=log_root)
    inspect_overrides = run_overrides(overrides)

    # before the capture and before the claim, because both of the things below
    # are cheap and one of them is a refusal. A five-minute Hawk capture that
    # ends in *put your API key in .env* is a worse version of the same message
    #
    # **This is the one write that precedes the archive gate**, and it is not a
    # write about the run: no manifest, no journal entry, no log moved. It has
    # to be here because the refusal on the next line names `.env` as the
    # remedy, and advice that leaks credentials into a commit is worse than no
    # advice (execution.md §8.3). Recorded at the moment it happens rather than
    # after the launch returns, so that a launch which then refuses — or raises
    # — still leaves the change accounted for
    if ignored := ensure_gitignore(workspace):
        steward_log(workspace.log, f"added to .gitignore: {', '.join(ignored)}")

    if timer and env_check:
        # the file a tend will actually load, which is not always this
        # workspace's own -- see `_timer.env.resolved`
        env_file = resolved_env(workspace.root)
        missing = unavailable_credentials(env_file, os.environ)
        if missing:
            raise LaunchError(explain_env(missing, env_file))

    outcome = acquire(workspace.claim, command="launch", break_stale=break_stale)
    if isinstance(outcome, Held):
        return outcome

    with outcome as claim:
        return _launch(
            workspace,
            definition,
            claim,
            args=args,
            type=type,
            accept_archive=accept_archive,
            timer=timer,
            interval=interval,
            log_root=root,
            log_store=store,
            overrides=inspect_overrides,
            reuse_overrides=reuse_committed(overrides, inspect_overrides),
            max_workers=max_workers,
            stall_after=stall_after,
            samples_ramp=samples_ramp,
            stuck_after=stuck_after,
            preauthorized=preauthorized,
            sync=sync,
            notification=notification,
            # resolved over the file here rather than left to the tend, because
            # the smoke gate below has to ask which model the fleet will scan
            # with. The tend re-resolves and lands on the same answer, since a
            # value that is no longer `None` wins its own precedence check
            scan_model=scan_model if scan_model is not None else directives.scan_model,
            scanners=directives.scanners,
        )


def _launch(
    workspace: Workspace,
    definition: Path,
    claim: Claim,
    *,
    args: dict[str, Any] | None,
    type: DefinitionType | None,
    accept_archive: bool,
    timer: bool,
    interval: int,
    log_root: str | None,
    log_store: str | None,
    overrides: EvalSetOverrides | None,
    reuse_overrides: bool,
    max_workers: int | None,
    stall_after: int | None,
    samples_ramp: tuple[int, int] | bool | None,
    stuck_after: int | None,
    preauthorized: dict[str, str] | bool | None,
    sync: str | bool | None,
    notification: str | bool | None,
    scan_model: str | bool | None,
    scanners: dict[str, Any] | None,
) -> Launch:
    """The launch itself, with the claim in hand for the whole of it."""
    committed = committed_manifest(workspace)
    manifest = capture_run(
        workspace,
        definition,
        committed,
        args=args,
        type=type,
        overrides=overrides,
        reuse_overrides=reuse_overrides,
    )
    # the merge is settled here, at the one moment capture's word and the
    # operator's are both in hand, and committed with the manifest: every
    # later tend injects exactly these (`Manifest.scan`)
    try:
        scan = scan_material(manifest.scan, scanners)
    except ScanError as ex:
        raise LaunchError(str(ex)) from ex
    manifest = manifest.model_copy(update={"scan": scan})
    # hoisted from the tend below, which makes exactly this call with exactly
    # this input a few lines later. The gate needs the model the fleet will
    # actually use rather than the flag alone, because the smoke recorded what
    # it exercised -- and reading it any other way would miss the environment
    # rung and report a change nobody made
    unrehearsed = _unrehearsed(workspace, manifest, establish_scan_model(scan_model))

    # resolved here and nowhere else, then carried by the manifest: a tend
    # re-deriving it would be re-reading an environment a scheduler does not
    # supply (`Manifest.log_dir`). Recorded even on the refusal path below,
    # where it is what the launch *would* have used
    log_dir = resolve_log_dir(workspace, manifest, log_root)
    manifest = manifest.model_copy(update={"log_dir": log_dir})
    # the *committed* manifest's directory, which is where the run's results
    # actually are. Ordinarily the same string and read once; when a `log_dir`
    # edit or a moved `log_root` has moved it, the difference is the whole of
    # what the delta would otherwise miss
    previous = _previous(workspace, committed, log_dir)

    # before the gate because it is a refusal and refusals come first; keyed
    # off the scan directory itself rather than `.steward/`, which is
    # deletable while the rows beside the logs are not
    try:
        verify_scan(
            scan,
            log_dir=log_dir,
            eval_set_id=manifest.eval_set_id,
            committed=committed.scan if committed is not None else None,
            committed_log_dir=previous,
        )
    except ScanError as ex:
        raise LaunchError(str(ex)) from ex
    except OSError as ex:
        raise LaunchError(
            f"the run's scan directory could not be read, so whether this "
            f"launch's scanners agree with what is already recorded is "
            f"unknown: {ex}"
        ) from ex

    inflight = resolve_inflight(workspace.inflight, workspace.workers)
    logs = _observe(log_dir)
    delta = compute_delta(
        manifest,
        committed,
        logs=logs,
        archived=_observe(archive_dir(log_dir)),
        running=inflight.running,
        stranded=_observe(previous) if previous != log_dir else None,
    )

    if not (delta.additive or accept_archive):
        # the refusal is the whole product of this call: the caller prints the
        # delta and the operator decides. Nothing has been written
        return Launch(manifest=manifest, delta=delta, unrehearsed=unrehearsed)

    try:
        write_manifest(manifest, workspace.manifest)
    except OSError as ex:
        raise LaunchError(
            f"the manifest could not be committed, so nothing has changed: {ex}"
        ) from ex

    failures: list[str] = []
    restored = _restore(workspace, delta, log_dir, failures)
    # rung 2 of the ladder, immediately after rung 1 and for the same reason
    # the restore is here: what a store read is *for* is the identifiers the
    # committed manifest asks for, and all `log_dir` mutation stays contiguous
    reused = _reuse(
        workspace, manifest, delta, logs, inflight.running, log_dir, log_store, failures
    )
    stopped = _stop(workspace, delta, inflight.running, logs, failures)

    # after the restore, keeping all `log_dir` mutation contiguous, and
    # before the first tend spawns anything: workers refuse to record into a
    # scan directory that does not exist, so a failure here fails the launch
    # rather than every worker identically
    scan_id = resolve_eval_set_id(log_dir, manifest.eval_set_id)
    try:
        scan_dir = initialize_scan(scan, log_dir=log_dir, scan_id=scan_id)
    except OSError as ex:
        raise LaunchError(
            f"the manifest is committed but the scan directory could not be "
            f"initialized, and workers refuse to record without it: {ex}"
        ) from ex

    armed, disarmed, refused = _supervise(workspace, interval, timer=timer)

    # after the arming so it can name the scheduler, and before the tend so
    # that turn's items already know this run was launched
    append_event(
        workspace.journal,
        LAUNCHED,
        definition=manifest.source.path,
        tasks=len(manifest.tasks),
        timer=armed.scheduler if armed is not None else None,
        log_store=log_store,
        # the source and not only the count: an identifier match says the
        # configuration was identical and says nothing about the environment
        # it ran in, so whose log this is has to be answerable later
        reused=[{"identifier": one.identifier, "source": one.source} for one in reused],
        scanners=sorted(merged_scanners(scan)),
    )
    if refused is not None:
        raise LaunchError(refused)

    turn = tend(
        workspace,
        max_workers=max_workers,
        stall_after=stall_after,
        samples_ramp=samples_ramp,
        stuck_after=stuck_after,
        preauthorized=preauthorized,
        sync=sync,
        notification=notification,
        scan_model=scan_model,
        claim=claim,
    )
    return Launch(
        manifest=manifest,
        delta=delta,
        committed=True,
        unrehearsed=unrehearsed,
        armed=armed,
        disarmed=disarmed,
        scan_dir=scan_dir,
        restored=restored,
        reused=reused,
        stopped=stopped,
        # `tend` cannot refuse a claim it was handed, so the union is only
        # formally present here
        turn=turn if isinstance(turn, TendResult) else None,
        failures=failures,
        pools=_pool_advice(workspace, manifest),
    )


def _unrehearsed(
    workspace: Workspace, manifest: Manifest, scan_model: str | None
) -> str | None:
    """Whether a passing smoke covers what this launch is about to run.

    **Two questions, because one answer cannot carry both.** Task identifiers say *which tasks* were rehearsed, and are the half that can name a number — *3 of 12 tasks* sends somebody somewhere, where a bare mismatch does not. They are also deliberately blind to how much of each task runs: `task_identifier` hashes execution limits and not the sample count, epochs or selection, so a dataset that doubled keeps every identifier while being a materially different night. The manifest digest is what closes that, and it is comparable at all only because **the rehearsal's slice rides its workers and never its capture** — a smoke that captured under its own `limit` would differ in the digest every time, for the one reason that does not matter.

    **Subset rather than equality on the identifiers**, because adding a task to a rehearsed definition is the case worth naming and removing one is not: what matters is that nothing about to run is unrehearsed.

    **So the digest is asked only where the two sets are equal**, which is the join between a subset rule and a whole-manifest hash. A capture that dropped a task hashes differently for that reason alone, and warning about a *shape* change there would report the removal twice under a name that does not describe it — while the removal itself is the case the identifier rule has already decided is fine. The cost is stated rather than hidden: a launch that both drops a task and grows another's dataset is not warned about the growth. That is an advisory line, and a rule a reader can hold is worth more than the corner it misses.

    **And the scan configuration, which no manifest digest covers.** `manifest_digest` hashes the tasks and the run's shaping fields, not `Manifest.scan` — so a scanner added after a passing smoke reviews every transcript of the run having been exercised on none, and a `scan_model` changed since is a model whose context window the rehearsal established nothing about. Compared through `scan_digest` rather than through the scanner *names*, because names are not the configuration: a parameter changed, a different scan-side model, a filter deciding which transcripts a scanner sees, all leave the names identical while changing what the rows say — the same silent drift the manifest digest refuses for tasks.

    A record written before any of these were carried reports `None` for them, and is compared on identifiers alone rather than being called stale — a fold that manufactured staleness out of its own absence of evidence would warn on every workspace rehearsed by an earlier version.

    **Never raises and never refuses, and *unknown* is a warning rather than a silence.** The journal not reading used to cost the warning outright, which reads the trade backwards: this check's whole output is advice, so warning when the answer cannot be established costs one line and staying quiet asserts *rehearsed* on no evidence at all.

    **Damage counts only past the last readable event**, which is the shape that actually hides a rehearsal rather than every torn line ever written. A crash mid-append leaves its fragment at the *end* of the file, exactly where the newest smoke would be, so the fold silently reads the pass before it and reports coverage that has since been superseded. Damage anywhere earlier cannot do that: whatever it destroyed is older than something that did read, and losing it can only make this fold report *less* coverage than the truth, which is the direction that warns rather than the direction that reassures. Treating both alike warned on every launch for the rest of the workspace's life over a line one crash tore months ago — and said the same words as `JOURNAL_DAMAGE`, which is the tend's item for it and the one place that can actually be cleared.

    Args:
        workspace: The workspace being launched.
        manifest: What this launch captured, with its scanners already merged in.
        scan_model: The model scanners will review with, as the fleet will have it.
    """
    identifiers = {task.identifier for task in manifest.tasks}
    if not identifiers:
        return None
    try:
        read = read_journal(workspace.journal)
    except OSError as ex:
        return f"the journal could not be read, so nothing here knows whether this was rehearsed ({ex})"
    if _torn_tail(read):
        return (
            "the journal's last line is damaged, so nothing here knows whether "
            "this was rehearsed — the newest smoke is where a torn line lands"
        )
    rehearsed = read_smoked(read.events)
    if not rehearsed.identifiers:
        return "no smoke has passed for this workspace"
    if missing := identifiers - rehearsed.identifiers:
        count = len(missing)
        return (
            f"the last passing smoke does not cover {count} "
            f"{'task' if count == 1 else 'tasks'} in this capture"
        )
    if (
        identifiers == set(rehearsed.identifiers)
        and rehearsed.digest is not None
        and rehearsed.digest != manifest_digest(manifest)
    ):
        return (
            "the last passing smoke rehearsed the same tasks at a different "
            "shape — the samples, epochs or selection have changed since"
        )
    if rehearsed.scanners is not None:
        if rehearsed.scanners != scan_digest(manifest.scan):
            return (
                "the last passing smoke scanned under a different configuration "
                "— the scanners, their parameters, or the filter and generation "
                "settings around them have changed since"
            )
        if rehearsed.scan_model != (scan_model or ""):
            return "the last passing smoke scanned with a different model"
    return None


def _torn_tail(read: JournalRead) -> bool:
    """Whether the journal's damage sits past everything that did read.

    Both numbers are line positions in the same file — `Event.line` is assigned by the reader and counted over *lines* rather than over parsed events, so a torn line keeps its number and does not renumber what follows it. Damage below the newest readable event destroyed something older than a record that survived; damage above it may be the record itself.

    A journal that is damage and nothing else has no readable event to be past, and reports `True` on the same argument: there is no surviving record for the fragment to be older than.
    """
    if read.intact:
        return False
    return max(line.line for line in read.damage) > max(
        (event.line for event in read.events), default=0
    )


def _pool_advice(workspace: Workspace, manifest: Manifest) -> PoolAdvice | None:
    """Whether to tell this operator about their Docker address pools, and once only.

    Asked after the run is committed rather than before, because the number to compare against is the provider's — `default_concurrency()`, which is what will actually be in force — and asking the provider means the answer tracks whatever inspect_ai is installed rather than a copy of its arithmetic that drifts.

    **Once per workspace**, recorded in the journal. The condition is a property of the host rather than of the run, so an operator who has heard it and decided against changing their daemon has answered for every later launch too, and repeating it on each one is how advice becomes something people learn to scroll past.

    **A journal that will not read costs the advice, and that is the opposite of what `_unrehearsed` does with the same failure.** The asymmetry is the point rather than an oversight: that one is a warning *about the run*, where saying nothing asserts something false — this one is a tip about somebody's Docker daemon, where saying nothing asserts nothing at all. What an unreadable journal actually costs here is the *once*, and repeating a tip somebody already declined is the failure mode this function exists to avoid. Either way it must not raise: this runs after the run is committed, and a launch that succeeded and then died rendering an aside is a launch nobody can tell succeeded.
    """
    try:
        advised = any(
            event.type == ACTION and event.payload.get("action") == POOLS_ADVISED
            for event in read_journal(workspace.journal).events
        )
    except OSError as ex:
        steward_log(workspace.log, f"could not read the journal for pool advice: {ex}")
        return None
    if advised:
        return None
    advice = advise(_wanted_sandboxes(manifest))
    if advice is not None:
        try:
            append_event(
                workspace.journal,
                ACTION,
                action=POOLS_ADVISED,
                networks=advice.networks,
                wanted=advice.wanted,
            )
        except OSError as ex:
            # said anyway, and said again next launch: the record is what makes
            # this once-only, and the advice is useful without it
            steward_log(workspace.log, f"could not journal the pool advice: {ex}")
    return advice


def _wanted_sandboxes(manifest: Manifest) -> int:
    """Concurrent sandboxes this run would use if nothing capped it.

    The definition's `max_sandboxes` where it declared one — a number somebody chose is what will be in force — and otherwise what the Docker provider itself would pick, asked of the provider rather than reimplemented. A fallback of twice the processors is the shape that default has always had, for an inspect_ai whose registry will not answer.
    """
    declared = manifest.options.get("max_sandboxes")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return declared
    try:
        from inspect_ai.util._sandbox.registry import registry_find_sandboxenv

        default = registry_find_sandboxenv("docker").default_concurrency()
    except Exception:
        default = None
    return default if default is not None else 2 * (os.cpu_count() or 1)


def capture(
    workspace: Workspace,
    definition: Path,
    *,
    args: dict[str, Any] | None,
    type: DefinitionType | None,
    overrides: EvalSetOverrides | None,
) -> Manifest:
    """Execute the definition and read the eval set out of it.

    **Public because a smoke captures too, and must capture identically.** A rehearsal that read the definition by any other route would be rehearsing a different enumeration than the launch it gates, and the two would then disagree about task identifiers — which is the one thing the gate compares.

    **Three paths matter, and conflating any two of them breaks something different.**

    `cwd` is the workspace root, because that is where `_fleet` pins every worker and the manifest records no cwd of its own — enumerating under one directory and executing under another is undetectable downstream, since nothing in the manifest says which was used.

    The path *read* is absolute. `read_eval_set` resolves what it is given against the **process's** cwd — it checks the file exists and hashes it before any subprocess with a `cwd` of its own is involved — so a relative path here means `steward launch` typed from a subdirectory of its own workspace cannot find its own definition.

    The path *recorded* is relative to the workspace root wherever the definition lives inside one, because `_tend.turn` resolves a relative one against the root. Absolute would work and would break the first time somebody moves or copies the workspace; relative to the shell's cwd would break immediately.

    So the manifest's `source.path` is rewritten after the capture rather than steered by the argument. `read_eval_set` keeps the simpler contract — *the path you hand me is the path I read and the path I record* — and the one caller with a reason to want them different is the one that states the reason.

    **The overrides go to capture rather than only to the workers**, and that is what makes the manifest describe the run. `epochs` and `limit` decide how many samples a task has, so an enumeration made without them would report per-task counts for a run nobody asked for — and every convergence check Steward performs is `samples × epochs` against a landed log. The document is a temporary file because the durable copy is the manifest capture writes: inspect records what it was given, so the fleet's later reads come from the artifact the enumeration was made under rather than from a second file that could drift from it.
    """
    absolute = definition.resolve()
    try:
        recorded = absolute.relative_to(workspace.root)
    except ValueError:
        # outside the workspace, which is legitimate — a shared definition read
        # by several workspaces — and can only be named absolutely
        recorded = absolute

    with TemporaryDirectory(prefix="steward-overrides-") as scratch:
        env: dict[str, str] = {}
        if overrides is not None:
            document = Path(scratch) / "overrides.json"
            document.write_text(overrides.model_dump_json(exclude_none=True))
            env[INSPECT_EVAL_SET_OVERRIDES] = str(document)
        try:
            manifest = read_eval_set(
                absolute, args=args, type=type, cwd=workspace.root, env=env
            )
        except (ValueError, ReadEvalSetError) as ex:
            raise LaunchError(str(ex)) from ex

    return manifest.model_copy(
        update={"source": manifest.source.model_copy(update={"path": str(recorded)})}
    )


def capture_run(
    workspace: Workspace,
    definition: Path,
    committed: Manifest | None,
    *,
    args: dict[str, Any] | None,
    type: DefinitionType | None,
    overrides: EvalSetOverrides | None,
    reuse_overrides: bool,
) -> Manifest:
    """Capture what the run *is*, reusing the committed manifest for whatever went unsaid.

    **One expression of the rule, because two would eventually differ.** A rehearsal resolving these three by any other route rehearses a different eval set than the launch it gates: a workspace launched with `-A` and `--epochs` would have its *defaults* rehearsed by a bare `launch --smoke` while the launch that follows reuses what was committed — the gate then comparing a run against a rehearsal of something else, and passing. That was measured rather than imagined, which is why the resolution lives here and both callers pass through it.

    Args:
        workspace: The workspace being captured for.
        definition: The definition to read.
        committed: Desired state as it stands, or `None` where nothing is committed.
        args: Definition arguments. `None` reuses the committed manifest's; an empty mapping asks for the definition's own.
        type: Explicit definition type, or `None` to reuse then detect.
        overrides: Inspect's words for this run, already resolved from flag and environment.
        reuse_overrides: Whether nothing named an override, in which case the committed manifest's stand. The caller decides this, because *nothing was typed and nothing was exported* is a fact about its own inputs.

    Returns:
        The captured manifest.
    """
    return capture(
        workspace,
        definition,
        args=args if args is not None else _prior_args(committed),
        type=type
        if type is not None
        else (committed.source.type if committed else None),
        overrides=_prior_overrides(committed) if reuse_overrides else overrides,
    )


def reuse_committed(
    given: dict[str, Any] | None, resolved: EvalSetOverrides | None
) -> bool:
    """Whether the committed manifest's overrides still stand.

    **Both halves, because either one alone gets a case wrong.** `_prior_overrides` is reached only when nothing said otherwise, and *otherwise* is a flag **or** a variable — its own docstring says any `STEWARD_*` and any `INSPECT_EVAL_*` replaces the set. Asking the flags alone discards a `STEWARD_EPOCHS` nobody typed but somebody exported, which on a first launch means the environment is read, resolved, and then dropped on the floor. Asking the resolved value alone cannot see `--no-overrides`, which resolves to nothing on purpose and must displace the committed manifest rather than fall back to it.

    Args:
        given: What the command line named, `{}` for `--no-overrides`, or `None` where nothing was typed.
        resolved: What the flag and the environment came to together.

    Returns:
        Whether to capture under the committed manifest's overrides.
    """
    return given is None and resolved is None


def committed_manifest(workspace: Workspace) -> Manifest | None:
    """Desired state as it stands, or `None` where this workspace has never launched.

    **A manifest that will not read raises rather than reading as absent.** Absent means every task is an addition and the delta needs no acceptance; unreadable means the same thing arrived at by accident, and would propose archiving a directory full of results nobody agreed to give up.
    """
    try:
        return read_manifest(workspace.manifest)
    except FileNotFoundError:
        return None
    except ManifestError:
        raise
    except OSError as ex:
        raise LaunchError(
            f"{workspace.manifest} could not be read, so what this run is "
            f"currently converging toward is unknown: {ex}"
        ) from ex


def _prior_args(committed: Manifest | None) -> dict[str, Any] | None:
    """The arguments the committed manifest was captured with, if any.

    What keeps a re-launch honest. `steward launch` typed a second time without the `-A` flags of the first would capture a *different* eval set and propose archiving everything the run has done — a data-losing outcome produced by forgetting a flag, which is precisely the shape of mistake the archive gate exists to catch and precisely the shape it should not have to.

    **Only reached when the caller said nothing about arguments.** A default with no way out is a trap, and this one had one: with `None` meaning *reuse* and any `-A` meaning *set*, a run that had once been launched with arguments could never be launched without them again. `--no-args` passes an empty mapping, which never arrives here.
    """
    if committed is None or not committed.source.args:
        return None
    return dict(committed.source.args)


def _prior_overrides(committed: Manifest | None) -> EvalSetOverrides | None:
    """Inspect's words as the committed manifest recorded them, if any.

    The same argument as `_prior_args`, one vocabulary over, and a stronger one. `--epochs 2` typed at the first launch is recorded in the manifest and carried to every later tend; typed a second time or not, the run's shape should not change. Dropped instead, a bare `steward launch` — the amend path, run to pick up an edited definition — would recapture the eval set at the definition's own epochs and limit, and every landed log would read as `reshaped` and re-run. That is the loss `--accept-archive` exists to catch, arriving through a gate it does not guard, because nothing left `logs/`.

    **Only reached when nothing said otherwise.** Any passthrough flag, any `STEWARD_*`, any `INSPECT_EVAL_*` replaces the whole set rather than merging into it — the same wholesale replacement `-A` performs, and for the same reason: a per-field merge would make *this run has no limit* unsayable. `--no-overrides` is the way back to the definition's own shape.
    """
    return committed.overrides if committed is not None else None


def _observe(log_dir: str) -> ObservedLogs:
    """Read a directory of logs, or say why it could not be read.

    Uncached, unlike a turn's read of the same directory. A launch happens once and has just spent minutes in a subprocess; the cache exists for the turn that runs every ten minutes, and warming it here would mean deciding what to do about the archive's entries, which no turn ever reads.
    """
    try:
        return observe_logs(log_dir)
    except OSError as ex:
        raise LaunchError(
            f"{log_dir} could not be read, so what this run has already done is "
            f"unknown and nothing can be committed against it: {ex}"
        ) from ex


def _restore(
    workspace: Workspace, delta: Delta, log_dir: str, failures: list[str]
) -> list[str]:
    """Move every restorable log back into the run's directory.

    A log that will not move costs a re-run of one task and nothing else, so it is recorded and stepped over — the same rule `_tend.turn._act` follows, for the same reason: one stuck file must not stop the rest of a launch.
    """
    restored: list[str] = []
    for row in delta.of(Change.RESTORE):
        for location in row.logs:
            try:
                restored.append(restore_log(location, log_dir))
            except OSError as ex:
                _failed(workspace, failures, f"could not restore {location}", ex)
    return restored


def _reuse(
    workspace: Workspace,
    manifest: Manifest,
    delta: Delta,
    logs: ObservedLogs,
    running: Sequence[RunningWorker],
    log_dir: str,
    location: str | None,
    failures: list[str],
) -> list[Reuse]:
    """Satisfy what is left from the store, rather than running it.

    **Rung 2 of the convergence ladder** (execution.md §5.6): the archive is free and local, the store is cheap and global, and a worker is neither. What reaches here is what rung 1 could not answer.

    **And what makes it work is that the copied log stops a spawn.** `reconcile._spawn_order` queues only `MISSING` and `INCOMPLETE` tasks, so a log that lands here leaves its task `COMPLETE` and nothing is ever started for it. That is precisely the property flow's own read half lacks — its workers run their selected task regardless, so a copied log bought nothing and cost a race (execution.md §5.3) — and it is why the read belongs to the single writer rather than to each worker.

    **Nothing here fails a launch.** A store whose absence costs time and never correctness cannot be allowed to cost a launch either, so an unopenable store, a query that raised and a copy that would not land are each one recorded line and one task that runs the ordinary way. `_restore`'s rule, and its reason.

    **And a task somebody is already running is not one this can satisfy.** A worker's log exists from the moment it starts, so the observation above accounts for the whole fleet — except across the pre-boundary window, where a process has been spawned, has no log and has no control socket yet (`RunningWorker.socket is None`). An identifier in that window looked exactly like an identifier nothing had ever run: the store answered it, the delta reported the work as satisfied and not running here, and the worker went on spending money on the task for as long as it took. `reconcile` already refuses to queue a running task on the strength of the same in-flight record; consulting it here is that rule applied one rung up the ladder, rather than a second one.

    **A worker being stopped outright is the exception**, and it is the case where reuse is worth most. Relocation and reshaping take the whole process down, so its tasks are about to start again from nothing — reusing a store result is then the difference between a re-run and no run at all. A *partial* stop needs no exception: the identifiers it sheds are leaving the manifest, so they were never in the wanted set.

    Args:
        workspace: The workspace being launched, for the log.
        manifest: What was just committed.
        delta: Its changes, for the restore rows this must not duplicate and the workers it is about to stop.
        logs: The log directory as it stood **before** the restore.
        running: The fleet as the in-flight record resolved it, for the tasks already under way.
        log_dir: Where a matched log is copied to.
        location: The store, or `None` where none is configured — in which case this is a no-op and not an error.
        failures: Accumulated warnings, appended to in place.

    Returns:
        One entry per task satisfied from the store.
    """
    if location is None:
        return []
    # already here, already coming back out of the archive, or already being
    # worked on. A restore that failed is deliberately *not* retried from the
    # store: rung 1 declining is a task that runs, which is the answer
    # `_restore` already chose for it
    wanted = (
        {task.identifier for task in manifest.tasks}
        - set(logs.attempts)
        - {row.identifier for row in delta.of(Change.RESTORE)}
        - _held(delta, running)
    )
    if not wanted:
        return []
    rows = {task.identifier: task for task in manifest.tasks}
    try:
        store = open_store(location, root=workspace.root)
        found = store.search(wanted)
    except StoreError as ex:
        _failed(workspace, failures, f"nothing could be reused from {location}", ex)
        return []
    reused: list[Reuse] = []
    for identifier, candidates in sorted(found.items()):
        task = rows[identifier]
        if (chosen := _chosen(workspace, manifest, task, candidates)) is None:
            continue
        try:
            landed = _copy_in(chosen, log_dir)
        # **broadly, and only `open_store` used to be.** `StoreError` covers
        # the search because `_store` normalizes its own failures; it covers
        # nothing after it, and what happens after it is reading and copying
        # files that live *in the store* -- so an S3 store on a machine whose
        # credentials expired between the query and the copy raised
        # `botocore.exceptions.NoCredentialsError` out of a launch that had
        # already committed its manifest. Rung 2 is an optimisation whose
        # every failure is one task that runs the ordinary way, and that has
        # to include the failures nothing here anticipated
        except Exception as ex:
            _failed(workspace, failures, f"could not copy {chosen} in", ex)
            continue
        reused.append(
            Reuse(
                identifier=identifier,
                key=task.key,
                source=chosen,
                location=landed,
            )
        )
    return reused


def _held(delta: Delta, running: Sequence[RunningWorker]) -> set[str]:
    """Identifiers a live worker is already working on, and this launch must not claim.

    Everything a running process holds, minus the processes this launch is about to take down outright — those are `relocated` and `reshaped`, which stop whatever they are running, so their tasks begin again from nothing and a store result is the difference between a re-run and no run. A worker losing only *some* of its tasks needs no exception here: what it sheds is leaving the manifest, and so was never wanted.
    """
    wholesale = delta.wholesale
    return {
        identifier
        for worker in running
        if worker.worker not in wholesale
        for identifier in worker.identifiers
    }


def _chosen(
    workspace: Workspace,
    manifest: Manifest,
    task: ManifestTask,
    candidates: Sequence[str],
) -> str | None:
    """The first of a store's candidates that answers what this run asks, or `None`.

    **A task identifier is not a promise about the results, and the store searches on nothing else.** `task_identifier` hashes the solver plan, generate config, model args, roles, version and execution limits — and pointedly *not* the sample count, the epochs or the selection, so that raising any of them leaves existing logs resumable rather than orphaning them. A store is therefore free to hand back a log for the same identifier that ran a different slice or fewer samples, and copying one in would leave the task `INCOMPLETE`, the next tend queuing it, and the launch having already said in the delta and the journal that the work does not run here.

    **Every candidate, because the store's own ranking cannot see the question.** It orders by size and recency, which is all a manifest-blind index can do — so the log it puts first may be the one that answers a different slice while the one behind it matches exactly. Checking only the front of the list turned this filter into a veto: it rejected the best-ranked log and never found out the store had what it was asked for.

    **The predicate is `observe`'s own**, not a second one assembled from the same ingredients. `incomplete_reason` is what `observe_tasks` classifies a task with, so a log this accepts is a log the next tend calls complete — which is the whole claim being made. A near copy of it drifted the way near copies do: it compared `completed_samples` where observation compares `total_samples`, so a signed log carrying samples an operator accepted as errored was publishable by one rule and unreusable by the other.

    A log that fails is not refused so much as not *claimed*. It stays in the store, where a run asking a different question will match it.
    """
    for source in candidates:
        try:
            candidate = read_attempt(source)
        # the store's own rule, applied at the store's own boundary: this reads
        # a file the store named and the store may be a remote one, so the
        # failures available here are the backend's hierarchy rather than
        # Python's. A candidate that will not read is one candidate skipped
        except Exception as ex:
            steward_log(workspace.log, f"{source} would not read from the store: {ex}")
            continue
        if (reason := incomplete_reason(manifest, task, candidate)) is None:
            return source
        # quietly, and to the log rather than to the launch: a store holding a
        # near-miss is worth being able to find out about and is not something
        # the operator did wrong
        steward_log(
            workspace.log,
            f"{source} matches {task.key} by identifier and does not answer what "
            f"this run asks of it ({reason.value}) — not reused",
        )
    return None


def _copy_in(source: str, log_dir: str) -> str:
    """Copy one log out of the store and into the run's directory.

    A copy rather than a move, which is the whole difference between this and the archive: the store keeps its own copy, and every other project reusing the same identifier gets it too.

    Args:
        source: The log the store matched, wherever it lives.
        log_dir: The run's log directory.

    **A name already taken is checked rather than believed.** Skipping on the name alone was right about the ordinary case — a name is a timestamp, a task and a hash, so it identifies the log — and wrong about the only case that reaches here at all. The wanted set was built from `observe_logs`, which *skips a log it cannot read*, so an identifier arrives here wanted precisely when the file under that name is one nothing could parse: the wreckage of an interrupted copy, sitting at the final path under the final name. That was then recorded as a task satisfied from the store, by a file no reader can open. `copy_log` compares it against the source and replaces it when it does not match, and stages every write so this can never be the file it leaves behind.

    Returns:
        Where the log now is.

    Raises:
        Exception: If the directory could not be created or the copy did not land. Not narrowed, and the caller does not narrow either: `source` is inside the store, which may be a remote one whose failures are its backend's rather than `OSError`.
    """
    fs = filesystem(log_dir)
    fs.mkdir(log_dir, exist_ok=True)
    target = log_dir.rstrip(fs.sep) + fs.sep + basename(source)
    copy_log(source, target, fs)
    return target


def _stop(
    workspace: Workspace,
    delta: Delta,
    running: list[RunningWorker],
    logs: ObservedLogs,
    failures: list[str],
) -> list[Stop]:
    """Stop the work the commit just made pointless.

    Two reasons, and `Delta.stopping` has already unioned them. **Its task left the manifest**: `reconcile` deliberately leaves an orphan's logs alone while something is still running it, so a worker left up here writes into `logs/` for hours against a definition nothing agrees with, and its logs are archived only once it stops of its own accord. **Or the log directory left underneath it**: a worker's destination is fixed in its selection document at spawn, so after a relocation the whole fleet is writing where nothing will look, and each task runs again from nothing once its worker exits.

    **What is stopped is tasks, not necessarily processes.** The two coincide at the default width and come apart once a run is packed: a worker holding one archived task and four live ones must lose the one and keep the four. Relocation is the exception and needs no subset — the directory moved out from under everything in the process.
    """
    wanted = set(delta.stopping)
    wholesale = delta.wholesale
    leaving = delta.leaving
    targets = [
        StopRequest(
            worker=worker,
            identifiers=(
                worker.identifiers
                if worker.worker in wholesale
                else tuple(
                    identifier
                    for identifier in worker.identifiers
                    if identifier in leaving
                )
            ),
        )
        for worker in running
        if worker.worker in wanted
    ]
    stopped = stop_workers(targets, locations=_locations(logs))

    for stop in stopped:
        if not stop.graceful:
            failures.append(
                f"worker {stop.worker} was {stop.outcome.value}: {stop.detail}"
            )
            steward_log(workspace.log, failures[-1])
    return stopped


def _locations(logs: ObservedLogs) -> dict[str, str]:
    """Log location to task identifier, which is how a control-channel row is named.

    A running task's log is created when it starts, so the observation this launch already read names every task the fleet is working on. The pre-boundary window is the exception, and a worker in it has no row to match either.
    """
    return {
        attempt.location: identifier
        for identifier, attempts in logs.attempts.items()
        for attempt in attempts
    }


def _supervise(
    workspace: Workspace, interval: int, *, timer: bool
) -> tuple[Armament | None, str | None, str | None]:
    """Put the run's supervision where the operator asked for it.

    **`--no-timer` disarms rather than merely skipping the arming**, and the difference only shows up on a re-launch. Skipping leaves the previous launch's scheduler entry firing every interval, `read_armed` still reporting it, and the operator told the opposite of what is true — while the run goes on scheduling expensive work against an explicit instruction not to. execution.md §8.3 says `--no-timer` *launches unsupervised*, which is a statement about the state the command leaves behind rather than about one step it omits.

    **Failing to disarm fails the launch, exactly as failing to arm does.** The asymmetry would be tempting — an arm that fails leaves a run nothing will tend, a disarm that fails leaves one something *will* — but both end with the command having said something untrue about who is watching, and between the two the unwanted timer is the one that spends money.

    Reported rather than raised, so that the `launched` event is written either way: a run whose manifest is committed *was* launched, and `status` has to be able to say it is unsupervised. The caller raises immediately afterwards.

    Args:
        workspace: The workspace.
        interval: Seconds between tends, for an arming.
        timer: Whether the run is to be supervised.

    Returns:
        What was armed, what was disarmed, and why neither happened.
    """
    if timer:
        try:
            return arm(workspace, interval), None, None
        except TimerError as ex:
            return (
                None,
                None,
                (
                    f"the manifest is committed but no timer could be armed, so "
                    f"nothing would tend this run: {ex}\nfix the above and run "
                    f"`steward timer arm`, or relaunch with --no-timer to drive it "
                    f"by hand"
                ),
            )
    try:
        return None, disarm(workspace), None
    except TimerError as ex:
        return (
            None,
            None,
            (
                f"the manifest is committed but the timer already armed here could "
                f"not be removed, so this run is still being tended on a schedule "
                f"despite --no-timer: {ex}\nfix the above and run `steward timer "
                f"disarm`"
            ),
        )


def run_overrides(given: dict[str, Any] | None) -> EvalSetOverrides | None:
    """Inspect's words for this run, resolved once and carried by the manifest.

    **Public for the same reason `capture_run` is**: a rehearsal that read the environment differently would rehearse a different shape. In particular `--no-overrides` arrives as an empty mapping and has to displace the environment as well as the committed manifest, which is a rule expressed here and nowhere else — a smoke doing its own `read_overrides(os.environ, given or {})` reads `STEWARD_*` and `INSPECT_EVAL_*` straight back in, and the flag silently does half of what it says.

    The flag, then `STEWARD_X`, then inspect's own variable, then the definition — the same shape as every Steward setting, one vocabulary over (`_workspace.overrides`). Resolved here rather than per turn because a run's shape is decided when it is launched: `tend` and `status` recompute Steward's own settings every turn and never re-decide what the eval set *is*.

    **Silence is not the same as nothing.** `None` here means no flag and, once the environment has been read, no variable either — which leaves the committed manifest's overrides in force (`_prior_overrides`). An empty mapping is `--no-overrides`, and displaces both.

    **`INSPECT_LOG_DIR` is refused rather than ignored.** Every other variable here is honoured because Steward is standing in for the CLI that documents it, and this one contradicts the answer Steward has already given: the run's logs go where the fleet is watched from. Honouring it would move a worker's output somewhere no tend reads, so every task would land and then read as never started; ignoring it would do the right thing while telling the operator nothing.
    """
    if os.environ.get(LOG_DIR, "").strip():
        raise LaunchError(
            f"{LOG_DIR} is set, and Steward decides where a run's logs go — the "
            f"fleet is watched from that directory, so a worker writing "
            f"elsewhere is a worker no tend can see. Set `log_dir` in your "
            f"definition instead, and unset {LOG_DIR} for this shell."
        )
    # an empty mapping is `--no-overrides`, which asks for the definition's own
    # shape -- so it displaces the environment as well as the committed
    # manifest, or it could not do what it says on a machine that exports one
    if given is not None and not given:
        return None
    try:
        return read_overrides(os.environ, given or {})
    except DirectivesError as ex:
        raise LaunchError(str(ex)) from ex


def _previous(workspace: Workspace, committed: Manifest | None, log_dir: str) -> str:
    """Where the committed run's results actually are.

    **Read back rather than recomputed, which is the whole of what makes a moved root visible.** Resolving both sides against the *current* `log_root` would compare a value with itself: every identifier survives a relocation, so the delta's rows would be empty, the gate would pass, and a launch would silently strand a directory full of results and re-run the sweep.

    A manifest committed before `Manifest.log_dir` existed carries none, and is resolved the way it was resolved then — without a root, since there were none.
    """
    if committed is None:
        return log_dir
    return committed.log_dir or resolve_log_dir(workspace, committed)


def _failed(
    workspace: Workspace, failures: list[str], what: str, ex: Exception
) -> None:
    """Record something that did not work, in both places it belongs."""
    failure = f"{what}: {type(ex).__name__}: {ex}"
    failures.append(failure)
    steward_log(workspace.log, failure)


__all__ = ["Launch", "LaunchError", "launch"]
