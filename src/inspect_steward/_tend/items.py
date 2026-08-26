"""The one list a turn produces, and the verdict over it.

Everything a turn wants to *say* beyond counts is an **item**: a stalled task, a definition that drifted, a file that would not read, later a parked worker or an open anomaly. Before this there were two hand-maintained lists of the same conditions — one in the CLI, one in `status.md` — which had already drifted apart in what they reported. One list, rendered twice, cannot.

**One list with an owner, not two lists.** The obvious split is *what a human must answer* against *what an agent should work on*, and it hard-codes a routing decision that is not Steward's to make: one workspace may let the agent rule on a class the next one reserves for a person. So `owner` is a field and the projections are a filter over it. It is a function of **(kind, state, policy)** and recomputed every turn, so changing the rules re-routes items that already exist. One kind will be fixed by design and policy may not widen it — a parked worker is always the human's, because nobody else may answer (agent.md, *What the agent may do without asking*).

**An item points at its subject; it does not contain it.** An anomaly carries nine fields of its own (workflow.md, *Anomalies are structured state*), and an envelope that absorbed them would be rewritten by every step that fills it. So `subject` is a task identifier, a pid, or a class key, and whoever owns that thing owns its shape.

**The id is the re-notification policy.** An acknowledgment is recorded against an id, so what makes an item worth saying again is exactly what changes its id — which turns the choice per kind into a design decision rather than a naming one. A stall keyed on its attempt count re-arms when the task fails again; drift keyed on the content hash re-arms on the next edit and clears at relaunch for free.

**Items are a projection, and *stays until resolved* comes from the subject.** A condition that ends stops being observed. Something needing a decision persists because the subject is still open. What neither covers is a real condition nothing will clear mechanically that somebody has already accepted — and that is what an acknowledgment is for.

**A summary names its task in full, where the table beside it does not.** That looks like an inconsistency and is the deliberate consequence of what each is for. A table row is a *comparison*, so `shorten_keys` elides whatever every row shares; an item is a *statement*, and it travels alone — into a channel, into a notification title, into a line somebody reads with no table under it. `sec_bench_pro` is enough to pick a row out of five; only the full key is enough to name a task to somebody who cannot see the other four.
"""

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING

from .._evalset.observe import ObservedTasks, TaskObservation, TaskState
from .._schedule import InFlight, attempts_made
from .._util.duration import format_duration
from .._workspace import Armed

if TYPE_CHECKING:
    # the turn assembles its result and then projects it, so the type it passes
    # can only be named here at type-check time
    from .turn import TendResult


class Owner(StrEnum):
    """Who resolves an item.

    Deliberately not a third value for Steward. Something the next turn will fix by itself is not an item — it is the line saying what the turn did.
    """

    AGENT = "agent"
    HUMAN = "human"


class Level(IntEnum):
    """How much an item is in the way. Ordered, because the verdict compares them."""

    INFO = 0
    ATTENTION = 1
    BLOCKING = 2
    """Nothing progresses on this subject until someone answers. No kind produces one yet — the parked worker is the first, and it arrives with step 20."""


class Verdict(StrEnum):
    """Where the run stands, as one glyph.

    Run-level rather than item-level: one blocked worker among twenty running is ⚠️, because a rule that paints red whenever any single thing is blocked makes red meaningless.
    """

    CLEAR = "✅"
    ATTENTION = "⚠️"
    STOPPED = "🛑"
    PAUSED = "⏸"


STALLED = "stalled"
DRIFT = "drift"
DEGRADED = "degraded"
ORPHAN_RUNNING = "orphan_running"
UNREADABLE = "unreadable"
ACTION_FAILED = "action_failed"
UNSUPERVISED = "unsupervised"
TIMER_DRIFT = "timer_drift"

OWNERS = {
    STALLED: Owner.HUMAN,
    DRIFT: Owner.HUMAN,
    DEGRADED: Owner.HUMAN,
    ORPHAN_RUNNING: Owner.HUMAN,
    UNREADABLE: Owner.AGENT,
    ACTION_FAILED: Owner.AGENT,
    UNSUPERVISED: Owner.HUMAN,
    TIMER_DRIFT: Owner.HUMAN,
}
"""Default owner per kind. Policy may move some of these once `_steward.md` can say so (step 23); a kind absent from the table is the agent's, since an unrouted item is an investigation rather than a question."""

UNACKNOWLEDGEABLE = frozenset({ACTION_FAILED})
"""Kinds that cannot be disposed of, because they have no lifecycle to dispose.

An action that failed is a single-turn fact: the next turn either hits it again or does not. Letting it be acknowledged would give it a persistence it does not have, and would silence a recurrence that happens to reuse the same words.
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
    """The interval `_steward.md` asks for, or `None` where it does not ask for one.

    **What the workspace *expressed*, never what a resolution produced.** `None` covers both a file that says nothing about intervals and one that would not parse, and in each case the right number of complaints is zero: an operator who armed a one-off `--interval 1m` against a file with no opinion has not created a conflict, and reporting one against Steward's own default would be reporting drift from a value nobody wrote. The same reasoning that keeps a `degraded` file from producing two items.
    """

    since_tend: float | None
    """Seconds since the previous recorded turn, or `None` where there has not been one.

    Read only by a `status`, and ignored by a tend. A turn asking how long it has been since a turn is asking about the gap *before itself*: on a schedule that is vacuous, and on a tend recovering from a long silence it is a fact about a condition that turn has just ended. The reader who needs telling that supervision stopped is the human typing `status` the next morning, not the timer that is evidently working.
    """

    since_armed: float | None
    """Seconds since the timer in force was armed, or `None` where none is.

    Here so that the silence attributed to a timer starts when the timer does. See `_silence`.
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

    @property
    def acknowledgeable(self) -> bool:
        return self.kind not in UNACKNOWLEDGEABLE


def tend_items(
    result: "TendResult",
    observed: ObservedTasks,
    inflight: InFlight,
    acknowledged: frozenset[str] = frozenset(),
) -> list[Item]:
    """Everything this turn has to say, as one list.

    Ordered by who has to act and then by how much it is in the way, so the projections do not have to sort and two renderings cannot disagree about precedence.

    Args:
        result: The turn, already reconciled and acted on.
        observed: The log directory read against the manifest — supplies display keys and attempt counts the summary does not carry.
        inflight: What is running, for the attempt counts that key a stall.
        acknowledged: Item ids somebody has already disposed of. Those are dropped entirely rather than marked — gone from the list and therefore from the verdict, every rendering, and the diff — with the journal event as the record (workflow.md, *The caveats that reached the final data*).

    Returns:
        Open items, acknowledged ones removed.
    """
    lookup = {task.identifier: task for task in observed.tasks}
    items = [
        *_stalled(result, lookup, inflight),
        *_drift(result),
        *_degraded(result),
        *_orphans(result, lookup),
        *_unreadable(observed),
        *_supervision(result),
        *_failures(result),
    ]
    items = [item for item in items if item.id not in acknowledged]
    return sorted(items, key=lambda item: (item.owner, -item.level, item.id))


def verdict(
    items: list[Item], *, paused: bool, running: int, spawning: int, unfinished: int
) -> Verdict:
    """Where the run stands.

    Four states, in the order they override each other. **Paused** wins outright: a run nobody is advancing is not making a claim about its own health. **Stopped** is the one that is *not* `max(level)` over the items — a run can contain nothing blocking and still be going nowhere, which is exactly what a fleet of stalled tasks is. So it is computed from the run rather than from the list: work left, nothing running, and nothing about to start.

    **`unfinished` is what keeps a completed run from reading as a stuck one.** A sweep that finished every task and left one unreadable file has nothing running and nothing to spawn, and it is not stopped — it is done, with a caveat. Only a run with work remaining can be stuck.

    Args:
        items: Open items, acknowledged ones already removed.
        paused: Whether the run is paused.
        running: Live workers.
        spawning: Workers this turn would start.
        unfinished: Manifest tasks not yet complete.

    Returns:
        The run's verdict.
    """
    if paused:
        return Verdict.PAUSED
    if not items:
        return Verdict.CLEAR
    if any(item.level >= Level.BLOCKING for item in items):
        return Verdict.STOPPED
    if unfinished and running == 0 and spawning == 0:
        return Verdict.STOPPED
    return Verdict.ATTENTION


HEADINGS = {Owner.HUMAN: "needs a person", Owner.AGENT: "for the agent"}
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
    if verdict is Verdict.PAUSED:
        return f"{verdict.value} paused — nothing new is being scheduled"
    if verdict is Verdict.CLEAR or not items:
        return f"{verdict.value} nothing needs you"

    human = sum(1 for item in items if item.owner is Owner.HUMAN)
    agent = len(items) - human
    parts: list[str] = []
    if human:
        parts.append(f"{human} {'needs' if human == 1 else 'need'} a person")
    if agent:
        parts.append(f"{agent} for the agent")
    counts = ", ".join(parts)

    if verdict is Verdict.STOPPED:
        return f"{verdict.value} nothing is progressing — {counts}"
    return f"{verdict.value} {counts}"


def by_owner(items: list[Item]) -> list[tuple[Owner, list[Item]]]:
    """Items grouped for rendering, in the order a reader should meet them.

    The human's first, always. An agent reading its own section second costs nothing; a person scrolling past the agent's work to find their own question is how a surface stops being read.

    Args:
        items: Open items.

    Returns:
        One entry per owner that has any, human first.
    """
    return [
        (owner, [item for item in items if item.owner is owner])
        for owner in (Owner.HUMAN, Owner.AGENT)
        if any(item.owner is owner for item in items)
    ]


def _stalled(
    result: "TendResult", lookup: dict[str, TaskObservation], inflight: InFlight
) -> list[Item]:
    items: list[Item] = []
    for identifier in result.summary.stalled:
        observation = lookup.get(identifier)
        key = observation.key if observation is not None else identifier
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
    if result.degraded is None:
        return []
    return [
        Item(
            id=f"{DEGRADED}:{result.degraded_at or 'unknown'}",
            kind=DEGRADED,
            owner=OWNERS[DEGRADED],
            level=Level.ATTENTION,
            subject="_steward.md",
            summary=(
                f"_steward.md could not be read, so this turn ran on the "
                f"settings the last one recorded ({result.degraded})"
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
                    f"asks for — stopping a worker is not a mechanical act"
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
                f"{_basename(log.location)} could not be read as a log ({log.reason})"
            ),
        )
        for log in observed.unreadable
    ]


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


def _supervision(result: "TendResult") -> list[Item]:
    """Whether anything is going to run the next turn.

    Only for a run with work left. A finished sweep needs no timer, and signing one off (step 26) disarms it deliberately — reporting that as lost supervision would make the last act of every run produce an item.
    """
    state = result.supervision
    if state is None:
        return []

    unfinished = sum(
        result.summary.states.get(task_state.value, 0)
        for task_state in (TaskState.MISSING, TaskState.INCOMPLETE)
    )
    if not unfinished:
        return []

    if state.armed is None:
        if not state.ever_armed:
            # never supervised is not the same as no longer supervised, and only
            # the second one is news
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
    # the disposition a person types hours later, and the only one for which
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


def _named(observation: TaskObservation | None, identifier: str) -> str:
    """A task, as an id can carry it: something readable, then something unique.

    A task identifier is a definition path, a name, an args hash, a model, and a config hash — around two hundred characters, two of them sha256. Putting one in an id would be correct and unusable: nobody can read it in a terminal and nobody will type it. So an id names the task and then pins it with the first eight hex of its identifier, which is short, stable across everything the identifier is stable across, and unique in any manifest a person is looking at.
    """
    name = observation.task.name if observation is not None and observation.task else ""
    if not name and observation is not None:
        # an orphan has no manifest row, so its log's task name is all there is
        name = observation.key
    readable = "".join(char for char in name if char.isalnum() or char in "._-")
    digest = sha256(identifier.encode("utf-8")).hexdigest()[:8]
    return f"{readable}:{digest}" if readable else digest


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
