"""The one list a turn produces, and the verdict over it.

Everything a turn wants to *say* beyond counts is an **item**: a stalled task, a definition that drifted, a file that would not read, a worker parked on an operator decision, later an open anomaly. Before this there were two hand-maintained lists of the same conditions — one in the CLI, one in `status.md` — which had already drifted apart in what they reported. One list, rendered twice, cannot.

**One list with an owner, not two lists.** The obvious split is *what an operator must answer* against *what an agent should work on*, and it hard-codes a routing decision that is not Steward's to make: one workspace may let the agent rule on a class the next one reserves for an operator. So `owner` is a field and the projections are a filter over it. It is a function of **(kind, state, policy)** and recomputed every turn, so changing the rules re-routes items that already exist. One kind is fixed by design and policy may not move it — a parked worker is always the operator's, because nobody else may answer (agent.md, *What the agent may do without asking*). See `FIXED_OWNER`.

**An item points at its subject; it does not contain it.** An anomaly carries nine fields of its own (workflow.md, *Anomalies are structured state*), and an envelope that absorbed them would be rewritten by every step that fills it. So `subject` is a task identifier, a pid, or a class key, and whoever owns that thing owns its shape.

**The id is the re-notification policy.** An acknowledgment is recorded against an id, so what makes an item worth saying again is exactly what changes its id — which turns the choice per kind into a design decision rather than a naming one. A stall keyed on its attempt count re-arms when the task fails again; drift keyed on the content hash re-arms on the next edit and clears at relaunch for free.

**Items are a projection, and *stays until resolved* comes from the subject.** A condition that ends stops being observed. Something needing a decision persists because the subject is still open. What neither covers is a real condition nothing will clear mechanically that somebody has already accepted — and that is what an acknowledgment is for.

**A summary names its task in full, where the table beside it does not.** That looks like an inconsistency and is the deliberate consequence of what each is for. A table row is a *comparison*, so `shorten_keys` elides whatever every row shares; an item is a *statement*, and it travels alone — into a channel, into a notification title, into a line somebody reads with no table under it. `sec_bench_pro` is enough to pick a row out of five; only the full key is enough to name a task to somebody who cannot see the other four.
"""

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING

from .._anomaly.model import (
    Anomalies,
    Anomaly,
    AnomalyState,
    Disposition,
    Proposal,
    Ruling,
)
from .._evalset.classify import kind_of, scan_task, short_token
from .._evalset.observe import ObservedTasks, TaskObservation, TaskState
from .._schedule import InFlight, Summary, attempts_made
from .._util.duration import format_duration, is_after, seconds_since
from .._worker import LiveParked, LiveStuck, acp_sockets
from .._workspace import DEFAULT_TEND_INTERVAL, Ack, Armed, Signature
from .progress import display_keys

if TYPE_CHECKING:
    # the turn assembles its result and then projects it, so the type it passes
    # can only be named here at type-check time
    from .turn import TendResult


class Owner(StrEnum):
    """Who resolves an item.

    Deliberately not a third value for Steward. Something the next turn will fix by itself is not an item — it is the line saying what the turn did.
    """

    AGENT = "agent"
    OPERATOR = "operator"


class Level(IntEnum):
    """How much an item is in the way. Ordered, because the verdict compares them."""

    INFO = 0
    ATTENTION = 1
    BLOCKING = 2
    """Nothing progresses on this subject until someone answers. `parked` is the only kind that produces one.

    **Precedence, not the verdict.** It says this item is in the way of *its own subject*, which is what puts it at the top of its owner's section; whether the *run* is stopped is arithmetic over the fleet and is computed separately (see `verdict`). A rule that painted a run red whenever any one thing was blocked would make red mean nothing.
    """


class Verdict(StrEnum):
    """Where the run stands, as one glyph.

    Run-level rather than item-level: one blocked worker among twenty running is ⚠️, because a rule that paints red whenever any single thing is blocked makes red meaningless.
    """

    CLEAR = "✅"
    ATTENTION = "⚠️"
    STOPPED = "🛑"
    PAUSED = "⏸"
    COMPLETE = "🏁"
    """Every task finished and nobody has accepted the results yet.

    Not ✅, which claims nothing is owed. A finished run owes the most consequential decision in the workflow, and reporting it as all-clear is how a sweep sits unread for a week. Distinct from ⚠️ for the opposite reason: nothing is wrong, and a warning glyph over a successful run trains a reader to discount warnings."""

    SIGNED_OFF = "🔒"
    """An operator accepted these results, and the signature still stands.

    **Terminal in a way no other verdict is**, which is why it is checked before the pause: everything else here describes a run that could still change, and a paused signed run is a signed run somebody stopped tending — reporting it as ⏸ would put the brake ahead of the attestation. The signature comes off the same way it went on, by something happening: a relaunch that changes the task set, or a window opening after it (workflow.md §13, *It can be invalidated*)."""


STALLED = "stalled"
STUCK = "stuck"
DRIFT = "drift"
DEGRADED = "degraded"
ORPHAN_RUNNING = "orphan_running"
UNREADABLE = "unreadable"
ACTION_FAILED = "action_failed"
UNSUPERVISED = "unsupervised"
TIMER_DRIFT = "timer_drift"
SIGNOFF_READY = "signoff_ready"
PARKED = "parked"
TUNING_PROPOSAL = "tuning_proposal"
UNWRITTEN = "unwritten"
JOURNAL_DAMAGE = "journal_damage"
STATUS_UNWRITABLE = "status_unwritable"
SYNC_FAILED = "sync_failed"
KILL_LOOP = "kill_loop"
ANOMALY = "anomaly"

OWNERS = {
    STALLED: Owner.OPERATOR,
    STUCK: Owner.OPERATOR,
    DRIFT: Owner.OPERATOR,
    DEGRADED: Owner.OPERATOR,
    ORPHAN_RUNNING: Owner.OPERATOR,
    UNREADABLE: Owner.AGENT,
    ACTION_FAILED: Owner.AGENT,
    UNSUPERVISED: Owner.OPERATOR,
    TIMER_DRIFT: Owner.OPERATOR,
    SIGNOFF_READY: Owner.OPERATOR,
    PARKED: Owner.OPERATOR,
    TUNING_PROPOSAL: Owner.OPERATOR,
    UNWRITTEN: Owner.AGENT,
    JOURNAL_DAMAGE: Owner.AGENT,
    STATUS_UNWRITABLE: Owner.OPERATOR,
    SYNC_FAILED: Owner.OPERATOR,
    KILL_LOOP: Owner.OPERATOR,
    ANOMALY: Owner.AGENT,
}
"""Default owner per kind. Policy may move some of these once `_steward.yaml` can say so (step 23); a kind absent from the table is the agent's, since an unrouted item is an investigation rather than a question."""

FIXED_OWNER = frozenset({PARKED})
"""Kinds whose owner policy may not move.

One entry, and it is the reason the module docstring says `owner` is a function of *(kind, state, policy)* with an exception. A park is a request for an operator decision about what an eval measures, and the agent may never answer one (agent.md §6) — so a `_steward.yaml` that routed it to the agent would not be expressing a preference, it would be asking Steward to answer an approval on an operator's behalf.
"""

UNACKNOWLEDGEABLE = frozenset(
    {ACTION_FAILED, PARKED, ANOMALY, SIGNOFF_READY, UNWRITTEN}
)
"""Kinds that cannot be disposed of, because they have no lifecycle to dispose.

An **unwritten** analysis is the one whose refusal is about the deliverable rather than the lifecycle. The item ends when the section has prose in it, and *looked, nothing here* is prose — a whole entry, worth as much as a finding (workflow.md §12.7). An acknowledgment would let the entry be waved past instead of written, which is the one outcome the item exists to prevent.

Readiness to sign is the newest member and the one that *changed* category. It was acknowledgeable while `steward signoff` did not exist — the only way an operator who had accepted the results could silence a reminder about a command they could not run. Now the command exists, and an ack would be somebody recording *I have decided* in the one place the decision is not: the run would go quiet with no signature, no curation, and nothing in `anomalies.md` marked final, which is precisely the silent certification workflow.md §13 opens by refusing.

An action that failed is a single-turn fact: the next turn either hits it again or does not. Letting it be acknowledged would give it a persistence it does not have, and would silence a recurrence that happens to reuse the same words.

A park is the opposite case and lands in the same set: it has a lifecycle, but the *only* thing that ends it is somebody answering. Acknowledging one would silence a worker still holding its slot, its sandbox and its model connections — which is the exact inverse of what an acknowledgment means everywhere else here, where it says *this is real and I have accepted it*. What the agent can do with a park is `raise` it, which is a claim about telling somebody rather than about the item being over.

An anomaly has a richer lifecycle than an ack can honour: its window closes on a **ruling** — `steward rule`, with a disposition, a reason, and an author — and never on somebody waving it past. The equivalent of accepting one is a `dismiss` ruling, which leaves a record where an ack would leave a silence (workflow.md §12.2).
"""


@dataclass(frozen=True)
class Supervision:
    """Whether anything is scheduled to tend this run, as the journal knows it.

    **Nothing here probes a scheduler.** A turn runs every ten minutes and `status` is meant to be cheap enough to type constantly, so asking `launchctl` would undo the header cache the step before this one built. What is compared instead is the arming Steward recorded against how long it has actually been since a tend — which is the better signal anyway, because it detects *not firing* whatever the cause, including a crontab somebody edited by hand.
    """

    armed: Armed | None
    """The timer the journal says is installed, or `None`."""

    ever_armed: bool
    """Whether one was ever installed.

    What keeps a hand-driven run quiet. A workspace that has never armed anything has not lost supervision — it never had any, and reporting that every ten minutes to somebody sitting at the terminal typing `steward tend` is how an attention list stops being read. The item is about an *expectation that broke*, so there has to have been one.
    """

    interval: int | None
    """The interval `_steward.yaml` asks for, or `None` where it does not ask for one.

    **What the workspace *expressed*, never what a resolution produced.** `None` covers both a file that says nothing about intervals and one that would not parse, and in each case the right number of complaints is zero: an operator who armed a one-off `--interval 1m` against a file with no opinion has not created a conflict, and reporting one against Steward's own default would be reporting drift from a value nobody wrote. The same reasoning that keeps a `degraded` file from producing two items.
    """

    since_tend: float | None
    """Seconds since the previous recorded turn, or `None` where there has not been one.

    Read by a `status` as a *report*: a turn saying how long it has been since a turn is, on a schedule, saying nothing, and on a tend recovering from a long silence it is describing a condition that turn has just ended. The reader who needs telling that supervision stopped is the operator typing `status` the next morning, not the timer that is evidently working.

    A tend reads it for the other thing it is: the width of the gap this turn answers for. Any threshold a turn crosses on somebody's behalf was crossed somewhere in here, so a notification that fires on the crossing has to measure against the real gap rather than the nominal cadence or a skipped turn loses it (`notify._newly_unattended`).
    """

    since_armed: float | None
    """Seconds since the timer in force was armed, or `None` where none is.

    Here so that the silence attributed to a timer starts when the timer does. See `_silence`.
    """

    ever_launched: bool = False
    """Whether anybody ever launched this run.

    The other way an expectation gets created, and the one the arming gate alone misses. `launch --no-timer` is a deliberate act with a deliberate consequence — execution.md §8.3 asks that an unsupervised run *look* unsupervised rather than looking like a healthy one — and it arms nothing, so on `ever_armed` alone it is indistinguishable from a workspace nobody has started. A launch is the moment somebody said *this run is meant to make progress*, which is exactly the expectation the item reports the breaking of.

    Defaulted, because a `Supervision` assembled by hand in a test is making a claim about timers and none about launches.
    """


@dataclass(frozen=True)
class Item:
    """One thing a turn has to say that is not a number."""

    id: str
    """Stable while the item means the same thing, and different once it does not. See the module docstring — this field is the re-notification policy, not merely an identity."""

    kind: str
    owner: Owner
    level: Level

    subject: str
    """What it is about: a task identifier, a pid, a class key. Not the thing itself."""

    summary: str
    """One line, in the present tense, naming what a reader would otherwise have to infer."""

    action: str | None = None
    """The command that resolves it, where one exists."""

    raised: bool = False
    """Whether the agent has put this in front of the owner who can decide it.

    The third item state, and it changes exactly one projection: `steward collect` sets a raised item aside and counts it, where `status` still shows it because an operator still owes an answer. Not a form of disposal — the item is as open as it was, and only the *agent's* work on it has ended (agent.md §2.2).
    """

    @property
    def acknowledgeable(self) -> bool:
        return self.kind not in UNACKNOWLEDGEABLE

    @property
    def addressable(self) -> bool:
        """Whether any command takes this item's id, which is what makes printing it useful.

        Not the same question as `acknowledgeable`, and they came apart with the park: `ack` refuses one because only answering clears it, while `raise` takes it, so its id has to be on screen. What is left over — unacknowledgeable *and* the agent's own — is `action_failed`, which nothing addresses because it is a single-turn fact, and whose id is a line number a reader should not be offered.
        """
        return self.acknowledgeable or self.owner is Owner.OPERATOR


def tend_items(
    result: "TendResult",
    observed: ObservedTasks,
    inflight: InFlight,
    acknowledged: frozenset[str] = frozenset(),
    raised: frozenset[str] = frozenset(),
) -> list[Item]:
    """Everything this turn has to say, as one list.

    Ordered by who has to act and then by how much it is in the way, so the projections do not have to sort and two renderings cannot disagree about precedence.

    Args:
        result: The turn, already reconciled and acted on.
        observed: The log directory read against the manifest — supplies display keys and attempt counts the summary does not carry.
        inflight: What is running, for the attempt counts that key a stall.
        acknowledged: Item ids somebody has already disposed of. Those are dropped entirely rather than marked — gone from the list and therefore from the verdict, every rendering, and the diff — with the journal event as the record (workflow.md, *The caveats that reached the final data*).
        raised: Item ids the agent has handed to their owner. **Marked rather than dropped**, which is the difference from an acknowledgment: the item is still open and an operator still owes an answer, so it stays in the list, the verdict, and `status`. Only the agent's own projection sets it aside.

    Returns:
        Open items, acknowledged ones removed and raised ones marked.
    """
    lookup = {task.identifier: task for task in observed.tasks}
    items = [
        *_stalled(result, lookup, inflight),
        *_parked(result, lookup),
        *_stuck(result, lookup),
        *_tuning(result),
        *_drift(result),
        *_degraded(result),
        *_orphans(result, lookup),
        *_unreadable(observed),
        *_unwritten(result),
        *_supervision(result),
        *_failures(result),
        *_journal_damage(result),
        *_status_unwritable(result),
        *_sync_failed(result),
        *_kill_loop(result),
        *_anomalies(
            result.anomalies,
            landed=frozenset(
                identifier
                for identifier, task in lookup.items()
                if task.state is TaskState.COMPLETE
            ),
            named=display_keys(result.progress),
        ),
        *_signoff(result),
    ]
    # **the filter is on acknowledgeable kinds, not on ids alone**, and the
    # narrowing is a migration rather than a tidy-up: `signoff_ready` was
    # acknowledgeable until the verb existed, so a workspace where somebody
    # silenced it in October holds an ack whose id still matches. Filtering on
    # the id alone would leave that run quiet forever and never offer it the
    # command it was waiting for
    items = [
        replace(item, raised=True) if item.id in raised else item
        for item in items
        if not (item.acknowledgeable and item.id in acknowledged)
    ]
    # owner first so an operator meets their own decisions before the agent's,
    # then level so that within an operator's own the ones costing something now
    # come before the ones that can wait (agent.md §4.1)
    return sorted(items, key=lambda item: (item.owner, -item.level, item.id))


def verdict(
    items: list[Item],
    *,
    paused: bool,
    running: int,
    spawning: int,
    unfinished: int,
    parked: int = 0,
    signed: bool = False,
) -> Verdict:
    """Where the run stands.

    Six states, in the order they override each other. **Signed off** wins outright and is checked first, ahead of the pause: it is the only terminal one, and a signed run that somebody then paused is a signed run — reporting the brake instead of the attestation would put the smaller fact in front. **Paused** is next, for its own version of the same reason: a run nobody is advancing is not making a claim about its own health. **Stopped** is the one that is *not* `max(level)` over the items — a run can contain nothing blocking and still be going nowhere, which is exactly what a fleet of stalled tasks is. So it is computed from the run rather than from the list: work left, and nothing *effectively* running or about to start.

    **A park subtracts from `running` rather than deciding the verdict.** `Level.BLOCKING` says an item is in the way of its own subject, which is a statement about ordering; the verdict is a statement about the run, and one blocked worker among twenty is a run that is working with a decision inside it. Twenty of twenty is the same arithmetic reaching zero — which is the sense in which enough parked workers stall a fleet and, at the ceiling, stop it.

    **`unfinished` is what keeps a completed run from reading as a stuck one.** A sweep that finished every task and left one unreadable file has nothing running and nothing to spawn, and it is not stopped — it is done, with a caveat. Only a run with work remaining can be stuck.

    **Complete is the last check rather than the first**, because it is the weakest claim here: it says the only thing left open is that nobody has accepted the results. Anything else open — a caveat, an unreadable file, a drift — outranks it, and the run reads as ⚠️ with the acceptance waiting inside it.

    Args:
        items: Open items, acknowledged ones already removed.
        paused: Whether the run is paused.
        running: Live workers.
        spawning: Workers this turn would start.
        unfinished: Manifest tasks not yet complete.
        parked: In-flight tasks with *nothing left running* — every one of their live samples is waiting on an operator. A task with one park among fifty working samples is still progressing and does not count here, though it still produces an item. Defaulted, because a caller assembling a verdict by hand is making no claim about parks.
        signed: Whether an attestation is in force, as `signed_off` decides it. Defaulted for the same reason `parked` is.

    Returns:
        The run's verdict.
    """
    if signed:
        return Verdict.SIGNED_OFF
    if paused:
        return Verdict.PAUSED
    if not items:
        return Verdict.CLEAR
    if unfinished and running - parked <= 0 and spawning == 0:
        return Verdict.STOPPED
    # a finished run whose only open decision is that nobody has accepted it is
    # not a warning. ⚠️ over a sweep that did exactly what was asked of it is how
    # a reader learns to discount the glyph, and ✅ would claim nothing is owed
    if all(item.kind == SIGNOFF_READY for item in items):
        return Verdict.COMPLETE
    return Verdict.ATTENTION


HEADINGS = {Owner.OPERATOR: "operator", Owner.AGENT: "agent"}
"""What each projection is called wherever items are grouped. One phrase, so the terminal, `status.md`, and eventually a channel post cannot describe the same filter differently."""


def verdict_line(verdict: Verdict, items: list[Item]) -> str:
    """The verdict as one line, which is also a notification's title (step 24).

    It has to stand alone, because in a channel it is the only part some readers see. So it says the state and who is holding it, and nothing about which item — a title naming one of five is worse than a title naming none.

    Args:
        verdict: The run's verdict.
        items: Open items, for the counts.

    Returns:
        One line, opening with the glyph.
    """
    return f"{verdict.value} {verdict_text(verdict, items)}"


def verdict_text(verdict: Verdict, items: list[Item]) -> str:
    """The same line without its glyph, for a caller that places one itself.

    Split out for the notification title, where the glyph leads the *workspace name* rather than the sentence — `🛑 my-sweep: the tend could not run` — so that a reader scanning a channel sorts on the first character and still learns which run it was. Here rather than by trimming `verdict_line`, because a renderer that took the glyph off a string by counting characters would be one emoji away from silently mangling the line.
    """
    if verdict is Verdict.SIGNED_OFF:
        return "signed off (the results were accepted)"
    if verdict is Verdict.PAUSED:
        return "paused (nothing new is being scheduled)"
    if verdict is Verdict.CLEAR or not items:
        return "nothing needs you"
    if verdict is Verdict.COMPLETE:
        return "complete (the results are waiting to be accepted)"

    human = sum(1 for item in items if item.owner is Owner.OPERATOR)
    agent = len(items) - human
    parts: list[str] = []
    if human:
        parts.append(f"{human} {'needs' if human == 1 else 'need'} an operator")
    if agent:
        parts.append(f"{agent} for the agent")
    counts = ", ".join(parts)

    if verdict is Verdict.STOPPED:
        return f"nothing is progressing, {counts}"
    return counts


def signed_off(
    signature: Signature | None,
    *,
    digest: str | None,
    anomalies: Anomalies,
    launched: str | None = None,
) -> bool:
    """Whether an attestation is in force over this run.

    Here rather than in `_signoff`, and not because it is convenient: three surfaces ask the question — the verdict, the readiness item, and the gate that refuses a second signature — and a predicate with three copies is a run that can read 🔒 while the verb offers to sign it again.

    Three conditions, all about what the signer could have known.

    **The digest** answers *is this the same set of results*. A project's definition evolves, so an attestation names what it covered (workflow.md §2.4) and a relaunch that changed the task set is no longer covered by it. Keyed on the manifest digest rather than the definition's hash for the reason the readiness item is: an edit sitting unlaunched changes the file and not the results, and a Flow spec relaunched with different arguments changes the results from a byte-identical file.

    **A launch un-signs whatever the digest says**, and the digest alone missed it. Relaunching an *unchanged* manifest produces the same digest — and it deliberately releases every acceptance latch (`turn._latched`), so an accepted short task starts running again while the old signature stands over it. A run reporting 🔒 with workers spawning into it is the attestation claiming something nobody attested to. The comparison is the latch's own, against the record `read_launched` already folds.

    **The window test is temporal, not "nothing is open now"**, and the difference decides a real case. A window that opened at 3am and was ruled at 4am is closed by the time anybody looks, and letting the old signature come back into force over a finding its signer never heard of is exactly the certification-by-default §13 opens by refusing. So a later finding un-signs permanently, even once it is ruled — and the remedy is the honest one, which is signing again.

    Args:
        signature: The most recent signature, or `None` where nobody has signed.
        digest: The committed manifest's digest now.
        anomalies: Every window, open and settled — the settled ones matter, because a window opened after the signature and ruled since is still a finding the signature did not cover.
        launched: When this run was most recently launched, or `None` where the caller makes no claim about launches.

    Returns:
        Whether the signature still stands.
    """
    if signature is None:
        return False
    if not signature.digest or signature.digest != digest:
        return False
    if launched is not None and is_after(launched, signature.ts):
        return False
    return not any(
        is_after(anomaly.opened_ts, signature.ts)
        for anomaly in (*anomalies.open, *anomalies.settled)
    )


def by_owner(items: list[Item]) -> list[tuple[Owner, list[Item]]]:
    """Items grouped for rendering, in the order a reader should meet them.

    The operator's first, always. An agent reading its own section second costs nothing; an operator scrolling past the agent's work to find their own question is how a surface stops being read.

    Args:
        items: Open items.

    Returns:
        One entry per owner that has any, human first.
    """
    return [
        (owner, [item for item in items if item.owner is owner])
        for owner in (Owner.OPERATOR, Owner.AGENT)
        if any(item.owner is owner for item in items)
    ]


def _stalled(
    result: "TendResult", lookup: dict[str, TaskObservation], inflight: InFlight
) -> list[Item]:
    items: list[Item] = []
    named = display_keys(result.progress)
    for identifier in result.summary.stalled:
        observation = lookup.get(identifier)
        key = named.get(identifier) or (
            observation.key if observation is not None else identifier
        )
        attempts = (
            attempts_made(observation, inflight) if observation is not None else 0
        )
        items.append(
            Item(
                # keyed on the attempt count, so a task that fails once more is
                # a new item rather than one already acknowledged
                id=f"{STALLED}:{_named(observation, identifier)}:{attempts}",
                kind=STALLED,
                owner=OWNERS[STALLED],
                level=Level.ATTENTION,
                subject=identifier,
                summary=(
                    f"{key} has stopped making progress after "
                    f"{attempts} {'attempt' if attempts == 1 else 'attempts'} "
                    f"and will not be respawned"
                ),
            )
        )
    return items


def _parked(result: "TendResult", lookup: dict[str, TaskObservation]) -> list[Item]:
    """A worker waiting on an operator, and the command that reaches it.

    **The one condition where walking away does not work.** Everything else Steward reports is either progressing or over; a parked sample is neither, and it holds its slot, its sandbox and its model connections while it waits. So it is the first kind to carry `Level.BLOCKING`, which orders it above everything else in its owner's section.

    **`Level.BLOCKING` is precedence and not the verdict.** One park among twenty running tasks is a run that is working with a decision inside it; only a fleet where nothing *can* move is 🛑. See `verdict`.

    **The tool function and nothing else.** A request's arguments and an `ask_user` prompt are model-generated text, and this line is relayed verbatim by an agent that then acts on it. A function name is structural; the rest is the eval's own output, and a summary is not the place to launder it into an instruction.
    """
    rows = [row for row in result.progress.rows if row.parked.total]
    if not rows:
        return []
    sockets = acp_sockets()

    items: list[Item] = []
    for row in rows:
        parked = row.parked
        socket = sockets.get(row.pid)
        items.append(
            Item(
                # keyed on the task alone: stable for as long as anything in it
                # is waiting, and gone once somebody has answered -- which is
                # exactly the edge a notification fires on (step 24). Not keyed
                # on the count or the functions, which would churn through a
                # resolve and an appear each time one of several parks cleared
                id=f"{PARKED}:{_named(lookup.get(row.identifier), row.identifier)}",
                kind=PARKED,
                owner=OWNERS[PARKED],
                level=Level.BLOCKING,
                subject=row.identifier,
                # *not* "nothing in it will progress until somebody answers",
                # which is what waiting means: a summary names what a reader
                # would otherwise have to infer, and that is the one clause they
                # would not. What they cannot infer is the cost of the wait
                summary=f"{row.key} {_waiting(parked)}, and it is holding a "
                f"worker while it waits",
                # the bare verb, and the socket only as proof there is anything
                # to reach: `--server` bypasses discovery, which is upstream's
                # answer for a *remote* machine, and here it would trade a
                # picker that floats waiting samples to the top for a path that
                # is per-pid and therefore stale the moment a worker respawns
                action="inspect acp" if socket is not None else None,
            )
        )
    return items


def _waiting(parked: LiveParked) -> str:
    """What a task is waiting for, as a predicate.

    Singular where there is one thing to name, because *1 sample waiting on 1 approval* is a count where a sentence would do. Plural once there are several, where the counts are the information.
    """
    if parked.total == 1:
        if parked.questions:
            return "is waiting on an answer to a question"
        if parked.functions:
            return f"is waiting on an approval for {parked.functions[0]}"
        return "is waiting on an approval"
    parts: list[str] = []
    if parked.approvals:
        plural = "" if parked.approvals == 1 else "s"
        functions = f" ({', '.join(parked.functions)})" if parked.functions else ""
        parts.append(f"{parked.approvals} approval{plural}{functions}")
    if parked.questions:
        parts.append(
            f"{parked.questions} question{'' if parked.questions == 1 else 's'}"
        )
    return f"has {parked.total} samples waiting on an operator: {' and '.join(parts)}"


def _stuck(result: "TendResult", lookup: dict[str, TaskObservation]) -> list[Item]:
    """A sample that has stopped moving inside a healthy worker, and the ladder's next rung.

    Not failed and not parked — a `bash` that never returns, a connection held open silently — which is why neither the anomaly queue nor the park can say it: nothing raised, and nobody is being asked anything. One item per task, whatever it holds, because the escalation is per task and a reader climbing the ladder wants one place to stand.

    **The id encodes the episode — what the item still asks about — plus `:asked`.** An acknowledgment is permanent per id (`read_acks` never expires one), so an id keyed on the task alone would let "I know, leave it" about this week's sample silence next week's forever. The digest is over the *un-asked* pending calls plus every call-less stuck sample (`_asks`): it re-arms on a different sample, when another call joins, and when rung 1 is spent on one call of several — the next call's ask is a new item the old acknowledgment does not cover, where a digest of the sample set alone would let acknowledging the first cancellation hide every rung-one action after it — and stays quiet while the same condition merely persists. The `:asked` flip is a new id for the same reason — the escalation re-notifies through the ordinary appeared diff, and an acknowledgment of the quiet wait does not cover the wedged one. The actions are execution.md §7.5's ladder, one rung at a time: `cancel-tool-call` costs one tool result, `cancel` costs the sample, and neither is ever pre-filled with an outcome — recording how a cancelled sample counts is the decision, so `--action` is left for the operator to type.

    **Owner is the agent only where `stuck_cancel` admits everything stuck and nothing has been asked yet** — rung 1 is the one pre-authorizable act, and once it has been spent the delivered-but-unheeded state is an operator's. The agent acts through `inspect ctl` itself and journals via the `ack --by agent` narrow exception; the tend never cancels anything.
    """
    items: list[Item] = []
    for row in result.progress.rows:
        stuck = row.stuck
        if not stuck.count:
            continue
        episode = _digest8(",".join(sorted(_asks(stuck))))
        suffix = ":asked" if stuck.asked else ""
        owner = (
            Owner.AGENT
            if not stuck.asked and _cancel_admitted(stuck, result.stuck_cancel)
            else OWNERS[STUCK]
        )
        items.append(
            Item(
                id=f"{STUCK}:{_named(lookup.get(row.identifier), row.identifier)}"
                f":{episode}{suffix}",
                kind=STUCK,
                owner=owner,
                level=Level.ATTENTION,
                subject=row.identifier,
                summary=_stuck_summary(row.key, stuck),
                action=_stuck_action(row.task_id, stuck),
            )
        )
    return items


def _asks(stuck: LiveStuck) -> set[str]:
    """The episode: every ask still open — un-asked pending calls by id, and the call-less stuck samples nothing can be asked of."""
    return {
        f"{one.sample_id}:{one.epoch}:{one.call_id}"
        if one.function
        else f"{one.sample_id}:{one.epoch}"
        for one in stuck.samples
        if not one.cancel_requested
    }


def _cancel_admitted(stuck: LiveStuck, granted: bool | tuple[str, ...] | None) -> bool:
    """Whether `stuck_cancel` covers everything stuck here — what hands rung 1 to the agent."""
    if not granted:
        return False
    return all(
        sample.function
        and (granted is True or sample.function in granted)
        and not sample.cancel_requested
        for sample in stuck.samples
    )


def _stuck_summary(key: str, stuck: LiveStuck) -> str:
    """What stopped, inside what, for how long — and explicitly what it is not."""
    functions = sorted({sample.function for sample in stuck.samples if sample.function})
    inside = f" inside {', '.join(functions)}" if functions else ""
    quiet = format_duration(max(60, int(stuck.oldest_idle)) // 60 * 60)
    if stuck.count == 1:
        line = (
            f"{key} has a sample that has stopped moving{inside} — no activity "
            f"for {quiet}, nothing failed and nothing is waiting on you"
        )
    else:
        line = (
            f"{key} has {stuck.count} samples that have stopped moving{inside} — "
            f"the oldest quiet for {quiet}, nothing failed and nothing is "
            f"waiting on you"
        )
    if stuck.asked:
        line += "; a cancel was asked and it did not stop"
    return line


def _stuck_action(task_id: str, stuck: LiveStuck) -> str | None:
    """The ladder's next rung, as one command — never two rungs at once.

    While any pending call is still un-asked, the command is rung 1 and only rung 1: `sample cancel` ends the whole sample and records an outcome, which is more than any `stuck_cancel` grant covers — so a sample wedged on several calls names one of them by id rather than escalating. Rung 2 appears only once rung 1 is spent (every call asked, and nothing stopped) or there was never a call to cancel — with `--action` left off, because recording how the cancelled sample counts is the decision. More than one stuck sample gets the listing, since a ladder is climbed one target at a time.

    Every interpolated value is shell-quoted. A sample id is the dataset's free text — the one field here Steward does not mint — and this line is one the runbook tells an agent to run: an id with a space would silently target the wrong sample, and worse is imaginable.
    """
    if not task_id:
        return None
    if stuck.count > 1:
        return shlex.join(["inspect", "ctl", "sample", "list", task_id, "--json"])
    sample = stuck.samples[0] if stuck.samples else None
    if sample is None:
        return None
    unasked = [
        one for one in stuck.samples if one.function and not one.cancel_requested
    ]
    if len(unasked) == 1 and len(stuck.samples) == 1:
        return shlex.join(
            [
                "inspect",
                "ctl",
                "sample",
                "cancel-tool-call",
                task_id,
                sample.sample_id,
                str(sample.epoch),
            ]
        )
    if unasked:
        first = unasked[0]
        return shlex.join(
            [
                "inspect",
                "ctl",
                "sample",
                "cancel-tool-call",
                task_id,
                first.sample_id,
                str(first.epoch),
                "--tool-call-id",
                first.call_id,
            ]
        )
    return shlex.join(
        [
            "inspect",
            "ctl",
            "sample",
            "cancel",
            task_id,
            sample.sample_id,
            str(sample.epoch),
        ]
    )


def _tuning(result: "TendResult") -> list[Item]:
    """Capacity tend has no authority to take, put in front of the one who could grant it.

    Two conditions with one shape (`_tend.tuning.Proposal`): a pinned setpoint holding a clean, saturated window, and a ramp at its ceiling with pushback still absent. Both mean the binding constraint is a number an operator chose, so the owner is the operator — and the agent's part is to relay it (`raise`) and to record the ruling for them (`ack`): "seen, happy at 60" is an acknowledgment, and the next level up would be a different item.

    **The summary says what is binding and at what number, and stops.** That it is the operator's to decide is what the item's owner already means, and *how* to decide it is the runbook's — repeating either in a sentence that appears in every post and every `status.md` costs a line each time to say something that never varies.

    The id carries the level, which is what makes an acknowledgment mean something narrow: capacity at 60 accepted is not capacity at 80 accepted, and a task the operator authorizes higher produces a fresh item the first time it holds a clean window at its new bound.
    """
    items: list[Item] = []
    for proposal in result.tuning.proposals:
        if proposal.pinned:
            summary = (
                f"{proposal.key} is saturated at its pinned max_samples of "
                f"{proposal.level} and the provider has headroom"
            )
        else:
            summary = (
                f"{proposal.key} is at the top of its samples_ramp "
                f"({proposal.level}) and pushback is still absent"
            )
        items.append(
            Item(
                id=f"{TUNING_PROPOSAL}:{_digest(proposal.identifier)}:{proposal.level}",
                kind=TUNING_PROPOSAL,
                owner=OWNERS[TUNING_PROPOSAL],
                level=Level.INFO,
                subject=proposal.identifier,
                summary=summary,
            )
        )
    return items


def _drift(result: "TendResult") -> list[Item]:
    if not result.drift:
        return []
    return [
        Item(
            # the hash is what changed, so a further edit re-arms this and a
            # relaunch clears it without anything having to remember either.
            # Truncated: a full sha256 in a line somebody has to read is worse
            # than the collision it prevents, which is not a real risk over the
            # handful of edits one run sees
            id=f"{DRIFT}:{_digest(result.definition_hash)}",
            kind=DRIFT,
            owner=OWNERS[DRIFT],
            level=Level.ATTENTION,
            subject=result.definition_hash or "",
            summary="the definition has changed since it was captured",
            action="steward launch",
        )
    ]


def _degraded(result: "TendResult") -> list[Item]:
    """Settings that would not parse, whichever spelling carried them.

    Worded around *the settings* rather than around the file, because a `STEWARD_*` variable can fail this way too and naming the file would send the reader to a document that is perfectly fine. The reason carries the specifics, and it already names the variable where one is at fault.
    """
    if result.degraded is None:
        return []
    return [
        Item(
            id=f"{DEGRADED}:{result.degraded_at or 'unknown'}",
            kind=DEGRADED,
            owner=OWNERS[DEGRADED],
            level=Level.ATTENTION,
            subject="_steward.yaml",
            summary=(
                f"this workspace's settings could not be read, so this turn ran "
                f"on the ones the last turn recorded ({result.degraded})"
            ),
        )
    ]


def _orphans(result: "TendResult", lookup: dict[str, TaskObservation]) -> list[Item]:
    items: list[Item] = []
    for identifier in result.summary.orphans_running:
        observation = lookup.get(identifier)
        key = observation.key if observation is not None else identifier
        items.append(
            Item(
                id=f"orphan:{_named(observation, identifier)}",
                kind=ORPHAN_RUNNING,
                owner=OWNERS[ORPHAN_RUNNING],
                level=Level.ATTENTION,
                subject=identifier,
                summary=(
                    f"{key} is still running work the definition no longer "
                    f"asks for (stopping a worker is not a mechanical act)"
                ),
            )
        )
    return items


def _unreadable(observed: ObservedTasks) -> list[Item]:
    return [
        Item(
            # per file rather than per count, so one bad file is one item and
            # acknowledging it does not silence the next one. Named by its
            # basename: a log directory is flat, so that is unique within it,
            # and the location is an absolute URI nobody wants to read or type
            id=f"{UNREADABLE}:{_basename(log.location)}",
            kind=UNREADABLE,
            owner=OWNERS[UNREADABLE],
            level=Level.ATTENTION,
            subject=log.location,
            summary=(
                f"{_basename(log.location)} could not be read as {log.what} "
                f"({log.reason})"
            ),
        )
        for log in observed.unreadable
    ]


def _unwritten(result: "TendResult") -> list[Item]:
    """Tasks whose `analysis.md` section carries facts and no reading of them.

    **The agent's standing work, and not a signoff blocker.** agent.md §6 lists writing this under what an agent does freely; holding an operator's attestation hostage to an agent's prose would be the wrong trade, and a run can be signed with the write-up still owed. What it does do is keep the run reading ⚠️ rather than 🏁, which is agent.md §4.3 exactly: unwritten is work outstanding rather than something that happened.

    **Only in a workspace an agent has actually attached to**, which is `Supervision.ever_armed`'s reasoning applied to the other kind of expectation. A run somebody drives by hand is owed no write-up by anybody — there is nobody the item is addressed to — and raising one would put every such run permanently at ⚠️ over work nobody agreed to do. The first `steward collect` is what creates the obligation.

    **Only once the task has stopped moving.** The section appears with the task's first log, which is right — the facts are worth keeping current from the moment there are any. The *item* used to appear then too, and that is a different claim: it asks somebody to explain numbers that are still changing. A four-task run put four of these up while every task was mid-flight and half the transcripts were unscanned, which is the same way an attention list stops being read that `analysis_md.analysis_sections` guards against one step earlier. Write-ups are owed on results, so the ask waits for results.

    **Finished means the same thing here as everywhere else** (`unfinished`): complete, or settled by an operator's decision. The second half matters more than it looks — a short-but-accepted task stays `INCOMPLETE` for good, deliberately, so gating on `COMPLETE` alone would mean the tasks with a known hole in them are the only ones never asked to explain it.

    **One item for the run, not one per task.** A write-up is owed per section and they clear one at a time, so this was N items — and on a four-task run that is four near-identical lines for what a reader experiences as a single piece of work, crowding out the anomaly sitting beside them. Nothing was bought with the granularity: `UNWRITTEN` is unacknowledgeable, so the per-task ids could not be disposed of individually and existed only to be printed. The tasks are named in the summary instead, and the item clears section by section exactly as before.

    The id is deliberately fixed rather than keyed on the set. It means the same thing every turn — *the write-up is outstanding* — so a run that finishes three of four sections has not acquired a new condition to notify anybody about.
    """
    if result.collected is None:
        return []
    settled = _final(result)
    owed = [
        key
        for identifier, key in sorted(result.unwritten.items(), key=lambda one: one[1])
        if identifier in settled
    ]
    if not owed:
        return []
    named = ", ".join(f"`{key}`" for key in owed)
    return [
        Item(
            id=UNWRITTEN,
            kind=UNWRITTEN,
            owner=OWNERS[UNWRITTEN],
            level=Level.ATTENTION,
            subject=owed[0] if len(owed) == 1 else "",
            summary=(
                f"{named} has no write-up in analysis.md — what the numbers "
                f"mean is still only in the numbers"
                if len(owed) == 1
                else f"{len(owed)} tasks have no write-up in analysis.md — what "
                f"the numbers mean is still only in the numbers: {named}"
            ),
            action="write the sections; *looked, nothing here* is an entry",
        )
    ]


def _final(result: "TendResult") -> set[str]:
    """Task identifiers that will not run again, by observation or by decision.

    Args:
        result: This turn.

    Returns:
        The identifiers. Empty where the turn observed no directory, which is a result assembled by hand rather than a run with nothing in it.
    """
    observed = result.observed
    if observed is None:
        return set()
    return {
        task.identifier for task in observed.tasks if task.state == TaskState.COMPLETE
    } | settled_by_decision(result.summary, result.acknowledged)


STALE_INTERVALS = 2
"""How many intervals may pass with no tend before supervision is called broken.

One would be a race with the timer itself — a turn that takes ninety seconds pushes the next one past its slot without anything being wrong. Two is the smallest number that cannot be produced by an ordinary slow turn, and a ten-minute timer silent for twenty minutes has missed one and is about to miss another.
"""


def _silence(state: Supervision) -> float | None:
    """How long the timer in force has had nothing to show for itself.

    Measured from the later of *the last recorded turn* and *this arming*, which is the smaller of the two ages. **A timer armed a minute ago has not been silent for the three hours before it existed**, and reporting that it has makes `steward timer arm` — the remedy the item itself names — look as though it did not work. Re-arming therefore resets the clock, and the item's id is keyed on the arming for the same reason.

    It also closes the other direction: a run armed and then never tended at all has `since_tend` of `None`, which on its own read as *no evidence of a problem* when it is the plainest case of one.

    Args:
        state: What the journal said about supervision.

    Returns:
        Seconds, or `None` where neither instant could be read.
    """
    ages = [age for age in (state.since_tend, state.since_armed) if age is not None]
    return min(ages) if ages else None


def settled_by_decision(summary: Summary, acknowledged: Mapping[str, Ack]) -> set[str]:
    """Tasks an operator has decided about, whether by ruling or by disposal.

    **Two acts, one meaning.** An `accept` ruling latches a task and says the results stand with a caveat; acknowledging a `stalled` item says *this will not be run again and the results stand without it*, which `anomalies.md` has been printing as a caveat in those words since the file existed. Only the first was counted, so an acknowledged stall was a hole the gate refused over forever — and the refusal's remedy is *rule the class*, which a stall need not have: the guard fires on attempt history, not on an anomaly, so there was no class to rule and no way to finish the run.

    Neither act changes what runs. The latch stops respawns the stall guard had already stopped; what both change is whether the run has anything left outstanding.
    """
    return set(summary.accepted) | {
        ack.subject
        for ack in acknowledged.values()
        if ack.kind == STALLED and ack.subject
    }


def unfinished(summary: Summary, acknowledged: Mapping[str, Ack] = {}) -> int:
    """Manifest tasks that neither finished nor were settled by a decision.

    One function because three callers read it and their disagreeing is the bug: `verdict` calls a run with nothing moving STOPPED, `_supervision` asks for a timer, and `_signoff` holds the invitation back. A task somebody has decided about is not work remaining in any of the three senses — Steward is not going to run it, nobody needs a timer for it, and the attestation covers it — so subtracting it once, here, is what keeps a signable run from reading 🛑 forever and nagging for a schedule it does not want.

    Args:
        summary: The reconciliation's account of the run.
        acknowledged: What has been disposed of, by item id — for the stalls among it (`settled_by_decision`). Empty for a caller with no journal, which then counts only the ruled ones.
    """
    return max(
        0,
        sum(
            summary.states.get(state.value, 0)
            for state in (TaskState.MISSING, TaskState.INCOMPLETE)
        )
        - len(settled_by_decision(summary, acknowledged)),
    )


def _supervision(result: "TendResult") -> list[Item]:
    """Whether anything is going to run the next turn.

    Only for a run with work left. A finished sweep needs no timer, and signing one off disarms it deliberately — reporting that as lost supervision would make the last act of every run produce an item.
    """
    state = result.supervision
    if state is None:
        return []

    if not unfinished(result.summary, result.acknowledged):
        return []

    if state.armed is None:
        if not (state.ever_armed or state.ever_launched):
            # never supervised is not the same as no longer supervised, and only
            # the second one is news. Either act creates the expectation: arming
            # once says a timer was meant to be here, and launching at all says
            # the run was meant to progress
            return []
        return [
            Item(
                # no discriminator: acknowledging this says *I am driving this
                # run by hand*, which stays true for as long as the condition
                # does. Arming a timer ends it, and disarming again is the same
                # statement rather than a new one
                id=UNSUPERVISED,
                kind=UNSUPERVISED,
                owner=OWNERS[UNSUPERVISED],
                level=Level.ATTENTION,
                subject="",
                summary=(
                    "no timer is armed, so nothing will tend this run until "
                    "somebody does it by hand"
                ),
                action="steward timer arm",
            )
        ]

    items: list[Item] = []
    armed = state.armed
    silence = _silence(state)
    # **Only a `status` may raise this, and a tend never may.** The age measured
    # is the gap before *this* turn, so on a tend that is recovering from a long
    # silence it is a past-tense fact stated in the present tense by the very
    # turn that disproves it -- recorded, then resolved next turn, which is
    # notification churn over a condition that has already ended. `status` is
    # the disposition an operator types hours later, and the only one for which
    # *nothing has tended in an hour* is still true when it is said
    if (
        not result.executed
        and silence is not None
        and silence > STALE_INTERVALS * armed.interval
    ):
        items.append(
            Item(
                # keyed on when this timer was armed, so re-arming asks the
                # question again and an acknowledged silence does not cover the
                # next timer's
                id=f"{UNSUPERVISED}:{armed.ts}",
                kind=UNSUPERVISED,
                owner=OWNERS[UNSUPERVISED],
                level=Level.ATTENTION,
                subject=armed.scheduler,
                summary=(
                    f"the {armed.scheduler} timer has not tended for "
                    f"{format_duration(int(silence))}, which is longer "
                    f"than {STALE_INTERVALS} intervals of "
                    f"{format_duration(armed.interval)}"
                ),
                action="steward timer status",
            )
        )

    if state.interval is not None and armed.interval != state.interval:
        items.append(
            Item(
                # both values, so changing the file again is a new question and
                # arming to match clears it
                id=f"{TIMER_DRIFT}:{armed.interval}:{state.interval}",
                kind=TIMER_DRIFT,
                owner=OWNERS[TIMER_DRIFT],
                level=Level.ATTENTION,
                subject=armed.scheduler,
                summary=(
                    f"the timer tends every {format_duration(armed.interval)} "
                    f"but this workspace now asks for "
                    f"{format_duration(state.interval)}"
                ),
                action="steward timer arm",
            )
        )
    return items


def _basename(location: str) -> str:
    """The last segment of a log location, which may be a path or an S3 URI."""
    return location.rstrip("/").rsplit("/", 1)[-1] or location


def _digest(value: str | None) -> str:
    """The distinguishing part of a hash, short enough to print."""
    if not value:
        return "unknown"
    return value.rsplit(":", 1)[-1][:12]


def _digest8(value: str) -> str:
    """Eight hex of a hash of anything, for an id built from text nobody should read back."""
    return sha256(value.encode("utf-8")).hexdigest()[:8]


def _named(observation: TaskObservation | None, identifier: str) -> str:
    """A task, as an id can carry it: something readable, then something unique.

    A task identifier is a definition path, a name, an args hash, a model, and a config hash — around two hundred characters, two of them sha256. Putting one in an id would be correct and unusable: nobody can read it in a terminal and nobody will type it. So an id names the task and then pins it with the first eight hex of its identifier, which is short, stable across everything the identifier is stable across, and unique in any manifest an operator is looking at.
    """
    name = observation.task.name if observation is not None and observation.task else ""
    if not name and observation is not None:
        # an orphan has no manifest row, so its log's task name is all there is
        name = observation.key
    readable = "".join(char for char in name if char.isalnum() or char in "._-")
    digest = _digest8(identifier)
    return f"{readable}:{digest}" if readable else digest


def _journal_damage(result: "TendResult") -> list[Item]:
    """Lines the journal yielded that could not be read as events.

    **The agent's, because the repair is judgement.** Damage is a torn last line after a crash — one record, recoverable by reading what remains of it — and every fold this turn ran (the pause, the acks, the diff baseline) ran without whatever the lines said. An operator cannot do anything with a line number; an agent can read the fragment and re-journal what it meant, or accept it as lost.

    One item for the damage as a whole, keyed on the line numbers, so further damage is a new question and an acknowledged tear stays acknowledged.
    """
    damage = result.journal_damage
    if not damage:
        return []
    lines = [entry.line for entry in damage]
    shown = ", ".join(str(line) for line in lines[:5])
    if len(lines) > 5:
        shown = f"{shown} and {len(lines) - 5} more"
    plural = len(damage) != 1
    return [
        Item(
            id=f"{JOURNAL_DAMAGE}:{_digest8(','.join(str(line) for line in lines))}",
            kind=JOURNAL_DAMAGE,
            owner=OWNERS[JOURNAL_DAMAGE],
            level=Level.ATTENTION,
            subject="journal.jsonl",
            summary=(
                f"{len(damage)} journal line{'s' if plural else ''} "
                f"(line{'s' if plural else ''} {shown}) could not be read as "
                f"event{'s' if plural else ''}, so whatever "
                f"{'they' if plural else 'it'} recorded is not being counted"
            ),
        )
    ]


def _status_unwritable(result: "TendResult") -> list[Item]:
    """`status.md` has stopped being writable, which only this can say quickly.

    The failure's natural signal is the file going stale, which a remote reader takes hours to notice and a local one never does. Keyed on the episode's start, so accepting one outage does not accept the next.
    """
    if result.status_failing is None:
        return []
    return [
        Item(
            id=f"{STATUS_UNWRITABLE}:{result.status_failing}",
            kind=STATUS_UNWRITABLE,
            owner=OWNERS[STATUS_UNWRITABLE],
            level=Level.ATTENTION,
            subject="status.md",
            summary=(
                f"status.md has not been writable since {result.status_failing}, "
                f"so every reader of it is seeing a snapshot frozen there"
            ),
        )
    ]


def _sync_failed(result: "TendResult") -> list[Item]:
    """A destination the workspace has stopped reaching.

    **Gated on one full tend interval**, which is what keeps a single slow bucket write from paging anybody: the propagation already retries every turn, and the episode worth an operator's attention is the one that outlived a retry. The interval is the armed timer's where there is one — the actual cadence of retries — and the default where the run is tended by hand.
    """
    interval = (
        result.supervision.armed.interval
        if result.supervision is not None and result.supervision.armed is not None
        else DEFAULT_TEND_INTERVAL
    )
    items: list[Item] = []
    for target, since in sorted(result.sync_failing.items()):
        age = seconds_since(since)
        if age is None or age < interval:
            continue
        items.append(
            Item(
                id=f"{SYNC_FAILED}:{_digest8(target)}:{since}",
                kind=SYNC_FAILED,
                owner=OWNERS[SYNC_FAILED],
                level=Level.ATTENTION,
                subject=target,
                summary=(
                    f"the workspace has not propagated to {target} since "
                    f"{since}, so a remote reader's copy is frozen there"
                ),
            )
        )
    return items


def _kill_loop(result: "TendResult") -> list[Item]:
    """Every turn is killing its wedged predecessor to take the claim.

    One break is the recovery working as designed and is not an item — it is a `steward.log` line and a history entry. A run of them is a tend that wedges deterministically, killed and reincarnated every interval, each incarnation destroying the evidence of the last; the run converges on nothing while every snapshot looks freshly tended. Keyed on when the run of breaks began, so a later, separate loop is heard again.
    """
    if result.breaks < 2 or result.breaks_since is None:
        return []
    return [
        Item(
            id=f"{KILL_LOOP}:{result.breaks_since}",
            kind=KILL_LOOP,
            owner=OWNERS[KILL_LOOP],
            level=Level.ATTENTION,
            subject="claim",
            summary=(
                f"every turn since {result.breaks_since} has had to kill a "
                f"wedged predecessor to take the claim ({result.breaks} in a "
                f"row) — the tend is wedging deterministically, not recovering"
            ),
        )
    ]


def _anomalies(
    anomalies: Anomalies,
    *,
    landed: frozenset[str],
    named: Mapping[str, str] = MappingProxyType({}),
) -> list[Item]:
    """Every open window's item, with owner following state.

    **The operator's items are worded in the eval's terms, not Steward's.** A proposal reaches them as *5 samples in cybench@gpt-5 (scoring artifact): the agent proposes to drop them from scoring — reason*, never as a proposal id acting on instances across classes. They know tasks, samples and scores; a window, a class key or a `prop-` id is a word from this codebase, and a summary built from those is one they cannot answer. `named` maps task identifiers to display keys for that sentence.

    **An OPEN `scan:` window produces no item until its task has landed.** A scan finding is decided when the task is done — the agent reads the task's flagged transcripts against the task's scores and puts every finding the task has to the operator at once — so a window on a running task is not yet anyone's work, and an item for it would start an investigation the runbook says to wait on. `landed` is the set of tasks in `TaskState.COMPLETE`; a rerun that reopens a task takes its windows back out of the queue until it lands again.

    The routing (workflow.md §12.5): an OPEN window is the agent's to investigate; an INVESTIGATING one is the agent's, informationally, so a fresh session does not re-open what the last one was mid-way through; a PROPOSED one is suppressed under **one consolidated item per live proposal** — the operator answers a decision, not a list; a RULED one produces nothing while the outcome is pending, because the tend applies the ruling itself and machinery in flight is not anyone's work; and a re-run that failed again is the operator's review, because it means the ruling's premise did not hold.

    **An OPEN `limit:` window produces no item at all.** An operator kill is somebody's own deliberate act, its window is adjudication material rather than an incident, and the run keeps going either way (workflow.md §15: anomalies are settled afterwards) — so it waits in the fold and the `### anomalies` block for the signoff conversation instead of asking inline. Engaging early still works: investigating, proposing, or ruling one puts it back on the ordinary surfaces.

    **The id is the re-notification policy** twice over here: the generation is in it, so a recurrence after a ruling is a new item over the precedent; and the weight rides in an order-of-magnitude bucket, so a population crossing 10, 100, 1000 re-arms the edge without every new instance being news.
    """
    items: list[Item] = []
    covered: dict[str, list[Anomaly]] = {}
    for anomaly in anomalies.open:
        if anomaly.state is AnomalyState.PROPOSED and anomaly.proposal:
            covered.setdefault(anomaly.proposal, []).append(anomaly)
            continue
        if anomaly.state is AnomalyState.OPEN and anomaly.kind == "limit":
            continue
        if anomaly.state is AnomalyState.OPEN and waiting_to_land(anomaly, landed):
            continue
        items.extend(_window_items(anomaly, named))
    for identifier, proposal in anomalies.proposals.items():
        windows = covered.get(identifier, [])
        if not windows:
            continue
        count = sum(window.evidence.count for window in windows)
        # the bucket rides here too: a proposed population crossing an order
        # of magnitude is news even while the question is already asked
        bucket = _bucket(count)
        items.append(
            Item(
                id=f"{ANOMALY}:prop:{identifier}" + (f":{bucket}" if bucket else ""),
                kind=ANOMALY,
                owner=Owner.OPERATOR,
                level=Level.ATTENTION,
                subject=identifier,
                summary=proposal_summary(proposal, windows, named),
                action=answer_command(
                    anomalies, [window.class_key for window in windows], named
                ),
            )
        )
    return items


def answer_command(
    anomalies: Anomalies, keys: Sequence[str], named: Mapping[str, str]
) -> str:
    """The `steward rule` an agent runs to record the operator's answer on these classes, by their shortest unambiguous tokens.

    `steward rule internet_egress grader_missing_tests`, not `steward rule --proposal prop-914eeddb`: the tokens are the findings' own names, so the agent carries nothing from the proposal to the answer but what it said to the operator. The proposal's disposition is the default; `--disposition` on the same line changes it.
    """
    open_keys = [anomaly.class_key for anomaly in anomalies.open]
    tokens = [
        short_token(open_keys, key, reserved=tuple(named.values())) for key in keys
    ]
    return "steward rule " + " ".join(_shell_word(token) for token in tokens)


def _shell_word(token: str) -> str:
    """A token as a shell takes it — bare where it is plain, quoted where the key's punctuation would not survive."""
    return token if re.fullmatch(r"[A-Za-z0-9_.:-]+", token) else f"'{token}'"


PLAIN = {
    Disposition.EXCLUDE: "to drop them from scoring",
    Disposition.ZERO: "to score them zero",
    Disposition.SCORE: "to keep their scores as recorded",
    Disposition.ACCEPT: "to keep them, with a note in the report",
    Disposition.RERUN: "to run them again",
    Disposition.DISMISS: "that nothing was wrong with them",
}
"""Each disposition as what it does to the samples, for a sentence an operator reads. The verb names are what `steward rule` takes and what the agent answers with; they are not what the operator is told."""


def proposal_summary(
    proposal: Proposal, windows: list[Anomaly], named: Mapping[str, str]
) -> str:
    """One sentence for a proposal, in the eval's words.

    `5 samples in cybench@openai/gpt-5 (scoring artifact): the agent proposes to drop them from scoring — the grader could not find its own tests`. The task by its display key, the finding by its label in words, the disposition by its effect, and the agent's reason verbatim — which is the whole of what the operator needs to answer, and nothing they have to look up.
    """
    count = sum(window.evidence.count for window in windows)
    tasks = sorted(
        {
            named.get(identifier, identifier)
            for window in windows
            for identifier in window.evidence.tasks
        }
    )
    labels = sorted(
        {anomaly_name(window.class_key).replace("_", " ") for window in windows}
    )
    where = f" in {', '.join(tasks)}" if tasks else ""
    who = "the agent" if proposal.by in ("", "agent") else proposal.by
    reason = f" — {proposal.reason}" if proposal.reason else ""
    return (
        f"{count} sample{'' if count == 1 else 's'}{where} ({', '.join(labels)}): "
        f"{who} proposes {PLAIN[proposal.action]}{reason}"
    )


def waiting_to_land(anomaly: Anomaly, landed: frozenset[str]) -> bool:
    """Whether a scan window's task is still running, which is what keeps the window out of the queue.

    One test shared by the queue and the listings, so a window the queue is silent about is described as waiting rather than as open. A window with no task named is not waiting: nothing would ever land it.
    """
    return (
        anomaly.kind == "scan"
        and bool(anomaly.evidence.tasks)
        and not set(anomaly.evidence.tasks) <= landed
    )


def precedent_line(ruling: Ruling, class_key: str) -> str:
    """One prior ruling as the line a decision is shown beside, naming the task where the ruling was on another task's window of the same finding."""
    line = f"{ruling.disposition.value} by {ruling.by} at {ruling.ts}: {ruling.reason}"
    if ruling.class_key != class_key and (task := scan_task(ruling.class_key)):
        line = f"{line} (for {task})"
    return line


def self_healing(item: Item) -> bool:
    """Whether an agent item resolves without anyone acting — what the no-agent escalation skips.

    A `task:` anomaly window is the one kind with a mechanical exit: Steward respawns the worker on its own, and the window resolves itself when the task completes (`_anomaly.fold`, the mechanical heal). Escalating one to a channel because no agent is attached would page an operator about something nobody needs to touch — and if it *doesn't* heal, the task stalls, and the `stalled` item is the operator-owned surface that says so durably.
    """
    return (
        item.kind == ANOMALY
        and item.owner is Owner.AGENT
        and item.subject.partition(":")[0] == "task"
    )


def _window_items(anomaly: Anomaly, named: Mapping[str, str]) -> list[Item]:
    """One window's item, worded as the finding rather than as the window.

    The summary is the sentence an agent would say to an operator — the task, the samples, what they did — and the class key rides in the action, where a verb needs it. A summary that led with the key put the key in every message the agent then wrote (workflow.md §12.5).
    """
    base = _anomaly_id(anomaly)
    key = anomaly.class_key
    summary = anomaly_summary(anomaly, named)
    if anomaly.state is AnomalyState.RULED:
        if anomaly.failed_resolutions:
            # the outcome *has* been observed, so the pending-outcome status
            # line would sit beside this contradicting it -- the review is the
            # whole story until a fresh ruling re-arms the pass
            detail = (
                anomaly.resolution.detail
                if anomaly.resolution is not None
                else "the re-run failed again"
            )
            return [
                Item(
                    id=f"{base}:failed{anomaly.failed_resolutions}",
                    kind=ANOMALY,
                    owner=Owner.OPERATOR,
                    level=Level.ATTENTION,
                    subject=anomaly.class_key,
                    summary=(
                        f"{finding_label(key)}: the samples re-ran and {detail} "
                        f"— the premise of the ruling did not hold"
                    ),
                    action=f"steward rule '{key}'",
                )
            ]
        # no item while the outcome is pending: the tend applies the ruling
        # itself now, so the window's pendency is machinery rather than
        # anyone's work -- the `### anomalies` block still says "awaiting the
        # re-run" for whoever asks
        return []
    if anomaly.state is AnomalyState.INVESTIGATING:
        note = f": {anomaly.note}" if anomaly.note else ""
        return [
            Item(
                id=f"{base}:investigating",
                kind=ANOMALY,
                owner=Owner.AGENT,
                level=Level.INFO,
                subject=key,
                summary=f"{summary} — under investigation{note}",
                action=f"steward propose '{key}' --action ... --reason ...",
            )
        ]
    return [
        Item(
            id=base,
            kind=ANOMALY,
            owner=Owner.AGENT,
            level=Level.ATTENTION,
            subject=key,
            summary=summary,
            action=f"steward propose '{key}' --action ... --reason ...",
        )
    ]


def _anomaly_id(anomaly: Anomaly) -> str:
    """`anomaly:<Name>:<digest8>:g<n>[:x<bucket>]` — readable, then unique, then the edges."""
    name = anomaly_name(anomaly.class_key)
    generation = f"g{anomaly.generation}"
    parts = [ANOMALY, name, _digest8(anomaly.class_key), generation]
    bucket = _bucket(anomaly.evidence.count)
    if bucket:
        parts.append(bucket)
    return ":".join(parts)


def _bucket(count: int) -> str:
    """The order-of-magnitude id segment, or empty below ten.

    The population's weight rides in the id so crossing 10, 100, 1000 changes it — a new item to the appeared-diff, hence one re-notification per magnitude rather than one per instance.
    """
    return f"x{10 ** (len(str(count)) - 1)}" if count >= 10 else ""


def anomaly_name(class_key: str) -> str:
    """The readable half of an anomaly id: the segment an operator will recognise.

    The exception's type where there is one (the segment carrying `@`), the task's name for a score class, the label for a scan class, the discriminating word otherwise — `vanished`, `no-log`, `operator`.

    A scan class takes its **label** rather than its scanner, because a run's scanners are few and its labels are what tell two findings apart: `anomaly:reward_hacking:…` is the id an operator recognises where `anomaly:scoring_integrity:…` would be the same word on every one of them. A scanner that sets no label falls back to its own name, which is then the only thing there is.
    """
    segments = class_key.split(":")
    for segment in segments:
        if "@" in segment:
            name = segment.partition("@")[0]
            break
    else:
        if segments[0] == "scan":
            # `scan:scanner:label:task:digest`, or four segments with no label
            name = segments[2] if len(segments) == 5 else segments[1]
        elif segments[0] == "score" and len(segments) >= 3:
            name = segments[2]
        else:
            name = segments[1] if len(segments) > 1 else segments[0]
    readable = "".join(char for char in name if char.isalnum() or char in "._-")
    return readable or "unclassed"


def class_summary(
    kind: str, class_key: str, count: int, *, tasks: Sequence[str] = ()
) -> str:
    """One sentence for a class of findings, from what a bare census already knows.

    **In the eval's words, without the class key.** *5 samples in cybench@gpt-5 flagged for reward hacking*, *2 samples errored the same way (TimeoutError)*: the task, the samples, what they did. The key is an address a verb takes, and a caller that has a verb to offer appends it; a sentence that carried it went into every message an agent wrote about the finding, and the operator reading that message has no idea what a class is.

    **Separated from `anomaly_summary` because a third reader arrived without a window.** The wording exists so that the decision queue and `anomalies.md` cannot describe one finding two ways; a smoke reports findings from a scan it just folded, before any window has been opened over them, and would otherwise have had to write a fourth phrasing of the same sentence. What a window adds — generation, prior rulings, the substrate warning — stays with `anomaly_summary`, since none of it is a fact a census holds.

    Args:
        kind: The class's kind, as `kind_of` names it.
        class_key: The class.
        count: How many instances.
        tasks: The tasks the instances are in, by display key, for a caller that has them. A scan class names its task itself, so a caller without display keys still gets one.
    """
    plural = "s" if count != 1 else ""
    name = anomaly_name(class_key)
    where = _where(tasks)
    if not where and kind == "scan" and (task := scan_task(class_key)):
        where = f" in {task}"
    if kind == "limit":
        return (
            f"{count} sample{plural}{where} {'were' if count != 1 else 'was'} "
            f"terminated by an operator"
        )
    if kind == "task":
        return f"{count} task attempt{plural}{where} {_task_failure(class_key)}"
    if kind == "score":
        task = tasks[0] if len(tasks) == 1 else name
        return f"every score in {task} converts to zero"
    if kind == "scan":
        return f"{count} sample{plural}{where} flagged for {name.replace('_', ' ')}"
    if kind == "scanerror":
        return (
            f"{count} transcript{plural}{where} could not be scanned, so "
            f"{'they carry' if count != 1 else 'it carries'} no verdict either "
            f"way ({_scan_failure(class_key)})"
        )
    return f"{count} sample{plural}{where} errored the same way ({name})"


def finding_label(class_key: str) -> str:
    """A class as a short noun phrase in the eval's words, for a line that names it without counting it.

    *reward hacking in cybench*, *TimeoutError errors*, *task attempts that vanished*: what a history line, a gate refusal or a verb's echo says before the key it says it about. The same reading of the key `class_summary` makes, minus the count.
    """
    kind = kind_of(class_key)
    name = anomaly_name(class_key)
    words = name.replace("_", " ")
    if kind == "scan":
        task = scan_task(class_key)
        return f"{words} in {task}" if task else words
    if kind == "scanerror":
        return f"scans that failed ({_scan_failure(class_key)})"
    if kind == "score":
        return f"every score zero in {name}"
    if kind == "limit":
        return "samples an operator terminated"
    if kind == "task":
        return f"task attempts that {_task_failure(class_key)}"
    return f"{name} errors"


def _where(tasks: Sequence[str]) -> str:
    """The *in which tasks* phrase, naming up to three and counting past that."""
    if not tasks:
        return ""
    if len(tasks) <= 3:
        return f" in {', '.join(tasks)}"
    return f" across {len(tasks)} tasks"


def _task_failure(class_key: str) -> str:
    """What a `task:` class says the attempts did, from the key's second segment."""
    segments = class_key.split(":")
    how = segments[1] if len(segments) > 1 else ""
    typed = anomaly_name(class_key) if "@" in class_key else ""
    if how == "vanished":
        return "vanished"
    if how == "no-log":
        return "left no log"
    if how == "no-log-exit":
        return f"exited without a log ({typed})" if typed else "exited without a log"
    return f"failed with {typed}" if typed else "failed"


def _scan_failure(class_key: str) -> str:
    """What broke a `scanerror:` class's scan — the exception where the key carries one, else the scanner."""
    if "@" in class_key:
        return anomaly_name(class_key)
    segments = class_key.split(":")
    scanner = segments[1] if len(segments) > 1 else "the scanner"
    return f"the {scanner} scanner"


def anomaly_summary(anomaly: Anomaly, named: Mapping[str, str] | None = None) -> str:
    """One sentence saying what happened to a class, plus what its window adds.

    Shared with `anomalies.md`, so the caveat list and the decision queue cannot word the same finding differently — and sharing its first half with the smoke digest, for the same reason one layer down. `named` maps task identifiers to display keys; given, the sentence says which tasks the window's instances are in.
    """
    tasks = (
        [named.get(task, task) for task in anomaly.evidence.tasks]
        if named is not None
        else []
    )
    line = class_summary(
        anomaly.kind, anomaly.class_key, anomaly.evidence.count, tasks=tasks
    )
    if anomaly.generation > 1:
        rulings = len(anomaly.precedent)
        line += (
            f" (generation {anomaly.generation}, "
            f"{rulings} prior ruling{'s' if rulings != 1 else ''})"
        )
    if anomaly.substrate:
        line += "; this looks like the machinery under the run — verify storage before re-running"
    return line


def _signoff(result: "TendResult") -> list[Item]:
    """The run has finished and nobody has accepted it.

    **The gap the verdict had.** A sweep whose every task completed reported ✅ *nothing needs you*, which is false in the one way that matters: the results exist and no operator has looked at them. Reporting a finished run as all-clear is how one sits unread for a week.

    **Worded as a state and carrying the command.** It said only the state for as long as `steward signoff` did not exist, because a surface telling somebody to run a command that does not exist is the same lie as a `_steward.yaml` key that parses and does nothing. Now there is a verb, so the sentence stays what is true and the action names what answers it — and the item stopped being acknowledgeable in the same move, since an ack would record *I have decided* in the one place the decision is not (`UNACKNOWLEDGEABLE`).

    **A standing signature closes it, and nothing else does.** The `signed` argument is the whole of that: the run reads 🔒, the invitation is gone, and both come off together when a relaunch or a later finding un-signs (`signed_off`).

    Fires on completeness alone rather than on *completeness and nothing else wrong* — a run that finished with an unreadable file beside it is still finished, and signoff accepts exceptions (workflow.md, *The attestation*) — **with one exception: an open anomaly**. Every anomaly resolved or accepted is workflow.md §12.2's definition of a resolvable run, so the readiness claim is simply false while a window is open, and a late finding un-readies the run for free: the window opens, this returns nothing, and the item disappears until somebody rules.

    An open `limit:` window deliberately does **not** hold this back. Operator kills are adjudication material — the very conversation this item invites — so gating the invitation on them would hide the one line that leads an operator to where they get ruled. The signoff verb itself still refuses while any window is open, which is the other half of the same split: the item invites the conversation, and the signature ends it.

    **A task can be settled by observation or by decision, and this counts both** — either decision, an `accept` ruling or an acknowledged stall (`settled_by_decision`). A log that is short-but-accepted stays `INCOMPLETE` forever, deliberately, because it *is* short and rewriting its results to say otherwise would put a number in the record nobody measured. So completeness alone can never be reached by a run with an accepted hole in it, and gating on completeness alone would make the one workflow §13 was written for — *accepting known holes must be explicit, not blocked* — the one workflow this item could never invite. Counted as a set, so a task somebody both ruled on and acknowledged is one settled task rather than two.
    """
    if result.signed:
        return []
    summary = result.summary
    decided = settled_by_decision(summary, result.acknowledged)
    settled = summary.states.get(TaskState.COMPLETE.value, 0) + len(decided)
    if not summary.tasks or settled != summary.tasks:
        return []
    if any(anomaly.kind != "limit" for anomaly in result.anomalies.open):
        return []
    return [
        Item(
            # **keyed on the task set the committed manifest asks for, not on
            # the definition's hash.** What is being accepted is a set of
            # *results*, and a file hash answers neither direction of that: an
            # edit sitting unlaunched would re-open a settled acceptance with
            # no new results behind it, and a relaunch that changed the tasks
            # without changing the file — different Flow arguments, a changed
            # import — would be silently covered by the old one
            id=f"{SIGNOFF_READY}:{_digest(result.manifest_digest)}",
            kind=SIGNOFF_READY,
            owner=OWNERS[SIGNOFF_READY],
            level=Level.INFO,
            subject=result.manifest_digest or "",
            summary=_signoff_summary(
                summary,
                len(decided),
                _dismissed_findings(result.anomalies),
                result.log_store,
            ),
            # bare, like every other item action: a placeholder here would be
            # one more thing for a reader to substitute, and this command's
            # arguments are the signer's name and their own words
            action="steward signoff",
        )
    ]


def _signoff_summary(
    summary: Summary, accepted: int, dismissed: int, store: str | None
) -> str:
    """What is true about the run, naming an accepted hole rather than papering over it.

    A run whose every task finished and one whose last task was accepted as it stands are both ready for the same decision, and they are not the same claim — so the invitation says which it is rather than reporting "every task is complete" over a log somebody knows is short.

    **Two clauses, not four.** *every task is complete (1 of 1) and nothing further will run, so the results are waiting to be accepted* said the same thing three ways: the parenthetical restates the sentence before it, *nothing further will run* is what *complete* means, and both sit directly above a table carrying the counts. This line is read on a phone at 3am and its job is to say a decision is owed.

    **And a third clause where scan findings were dismissed**, which is the one thing on this line that is not about task counts. A dismissed finding leaves no caveat and reaches `anomalies.md` nowhere — correctly, since the whole content of the dismissal is *this does not change the numbers*. But *the model tried to read the grader and failed* is something the operator signing wants to have been told, and this is the sentence that reaches them at the moment they are asked (workflow.md §12.6.1). It says how many and points at the account; the reasons are in the journal and in `analysis.md`.

    **And a fourth where a store is configured, which is a second decision rather than a fact about the run.** Publication is the one act at the end of a run that nothing does by default and no setting can turn on: exporting results into a cache other projects read is an operator's call, taken once and out loud. So this line is the whole mechanism by which they are asked — an agent that is not told there is a store is an agent that signs off without mentioning it, and the run never tends again to say so afterwards.

    Args:
        summary: The run's shape.
        accepted: Tasks settled by a decision rather than by finishing (`settled_by_decision`), which is a count the summary cannot supply on its own: an acknowledged stall settles a task and is recorded in the journal rather than in the reconciliation.
        dismissed: Scan findings looked at and dismissed (`_dismissed_findings`).
        store: The configured reuse store, or `None` where there is none and there is nothing to ask about.
    """
    complete = summary.states.get(TaskState.COMPLETE.value, 0)
    if not accepted:
        line = "every task is complete; the results are waiting to be accepted"
    else:
        line = (
            f"{complete} of {summary.tasks} tasks are complete and {accepted} "
            f"accepted as {'it' if accepted == 1 else 'they'} "
            f"stand{'s' if accepted == 1 else ''}; "
            f"the results are waiting to be accepted"
        )
    if dismissed:
        one = dismissed == 1
        line = (
            f"{line} ({dismissed} scan finding{'' if one else 's'} "
            f"{'was' if one else 'were'} looked at and dismissed — read the "
            f"reason{'' if one else 's'} before you sign)"
        )
    if store is None:
        return line
    return (
        f"{line}. A log store is configured at {store}: ask whether these "
        f"results should be published to it, and pass --publish if they should"
    )


def _dismissed_findings(anomalies: Anomalies) -> int:
    """Instances of settled `scan:` windows a ruling dismissed.

    Counted rather than journalled: a dismissal is already a `ruling` event, so deriving the number from the fold keeps one record of the decision and no second one that could disagree with it.
    """
    return sum(
        anomaly.evidence.count
        for anomaly in anomalies.settled
        if anomaly.kind == "scan"
        and anomaly.ruling is not None
        and anomaly.ruling.disposition is Disposition.DISMISS
    )


def _failures(result: "TendResult") -> list[Item]:
    return [
        Item(
            id=f"{ACTION_FAILED}:{index}",
            kind=ACTION_FAILED,
            owner=OWNERS[ACTION_FAILED],
            level=Level.ATTENTION,
            subject="",
            summary=failure,
        )
        for index, failure in enumerate(result.failures)
    ]
