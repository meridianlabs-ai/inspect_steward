"""The journal: the one record in a workspace that nothing can rebuild.

A manifest re-captures from the definition, anomalies re-derive from the log directory, the in-flight record rebuilds from the process table — but a ruling and its reasoning exist nowhere else. So the append here is flushed to disk, and the read is written for the situation after a crash rather than the ordinary one.

The mechanics live in `_util.jsonl`, which the in-flight record shares: single-write appends, damage costing one line, and an unrecognised type reading as data rather than as an error. What is journal-specific is the durability (`sync=True`, the default) and the vocabulary below.

State is derived from this file rather than stored beside it (workflow.md, *State is a fold over the journal*), which is what makes crash recovery the normal code path instead of a rescue routine nobody tests.
"""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .._util.jsonl import (
    DamagedLine,
    Event,
    EventRead,
    append_event,
    read_events,
    utc_now,
)

__all__ = [
    "ACKNOWLEDGED",
    "ARMED",
    "COLLECTED",
    "DISARMED",
    "LAUNCHED",
    "OBSERVATION",
    "PAUSED",
    "RAISED",
    "RESUMED",
    "Ack",
    "Armed",
    "Collected",
    "DamagedLine",
    "InitializedEvent",
    "JournalEvent",
    "JournalRead",
    "JournalSummary",
    "LaunchedEvent",
    "Paused",
    "Raised",
    "append_event",
    "read_acks",
    "read_armed",
    "read_collected",
    "read_journal",
    "read_launched",
    "read_pause",
    "read_raised",
    "summarize",
    "utc_now",
]

OBSERVATION = "observation"
"""Journal event: what one turn saw, and the settings it saw it under.

Written by every executed turn (`_tend.turn`), whether or not anything happened, because an agent reads the run as a **time series** and its own memory does not survive a session boundary — there are several of those in a night. If the series is not written down it does not exist, and the 6am agent inherits a list of open items with no idea which are getting worse (workflow.md, *The journal records observations, not only decisions*).

It is also what makes degrading `_steward.md` possible: the settings in force are recorded here, so a turn that cannot parse the file has somewhere to read the last good ones from.

Named here with the other event types rather than beside its writer because two folds read it: the next turn diffs its `items` to find what appeared and what resolved, and `read_raised` uses the same list to expire a hand-off whose item is gone. It is the only record of a condition having *stopped* being true.
"""

ACKNOWLEDGED = "acknowledged"
"""Journal event: somebody looked at an item and accepted it.

The one event kind an item list was not supposed to need. Items are a projection, so a condition that ends stops being reported and a decision keeps its subject open — but neither covers a real condition nothing will clear mechanically that somebody has already accepted. Without this, a definition edited on purpose reports drift every ten minutes for the rest of the run, which is how an attention list stops being read.

Written by `steward ack`, never by a tend. It carries `id`, `by` (`agent` or `human`), and a required `reason` — the discipline `inspect ctl` already imposes on every applied change, and what makes *who decided, and why* (workflow.md, *The audit trail*) true of this act too.
"""

RAISED = "raised"
"""Journal event: the agent put an item in front of the person who can decide it.

**It closes nothing, and that is the whole point.** An item owned by a human stays open until a human rules on it — but the *agent's* work on it ended when it was surfaced, and without a record of that the item returns to the agent's queue at every collection all night. Reading is the wrong verb for something the reader cannot dispose of (agent.md §2.2), so raising is the verb.

The third of three item states, between *needs the agent* and *closed*. What it changes is one projection: `steward collect` sets a raised item aside and counts it; `status` still shows it, because a person still owes an answer.

Carries `id` and an optional `note` — optional where `acknowledged`'s reason is required, because disposing of a decision owes an account and handing one off does not.
"""

COLLECTED = "collected"
"""Journal event: how far an agent has read.

A **position and nothing else** — no note, no claim about having acted. An event that asserts two things at once is eventually read as the wrong one, and what was *done* is recorded by `acknowledged`, `raised`, and `action` at the moment it was done.

The position is a journal line number (`Event.line`), which is assigned when the file is read rather than written into it — see that property for why a stored sequence number would race.

Two things read this fold: the delta an arriving agent is shown, and the collection age, which sits beside the tend age so that *the timer stopped* and *nobody is looking* are distinguishable (agent.md §2.2).

Carries `position`.
"""

PAUSED = "paused"
"""Journal event: stop scheduling new work.

**Here rather than in `.steward/`, and the difference is a safety property.** `.steward/` is disposable by construction — deleting it is documented as costing nothing — so a pause flag living there means clearing a cache silently *resumes* an expensive run overnight. Between the two directions this can fail in, a pause that outlives a wiped cache is recoverable and a resume nobody asked for is not.

Carries `by` (`agent` or `human`) and a required `reason`. A tend never writes it.
"""

RESUMED = "resumed"
"""Journal event: schedule again.

No reason, unlike its opposite: pausing asserts something about the run that a later reader will want explained, and resuming only restores the default.
"""

ARMED = "armed"
"""Journal event: a timer was installed, and by which scheduler.

What makes *the timer is not running* detectable at all. A scheduler cannot report its own absence, and probing one costs a subprocess on every turn — so the fact that a timer was installed is recorded once, here, and every later turn compares that record against how long it has actually been since a tend (`_tend.items`, `unsupervised`).

Carries `scheduler`, `interval` in seconds, and `label`.
"""

DISARMED = "disarmed"
"""Journal event: the timer was removed. Carries `scheduler`."""

LAUNCHED = "launched"
"""Journal event: somebody started this run, and what they started it with.

**Not `_worker.inflight`'s `launched`**, which is the same word about a different subject — that one says a worker's spawn returned, and lives in a rebuildable record. This one says a *run* was launched, and is here because losing it would change what Steward reports.

What it buys is one item's correctness. `unsupervised` is gated on a timer having been armed, deliberately, so a workspace nobody armed stays quiet rather than nagging somebody sitting at the terminal typing `steward tend` (`_tend.items`). `launch --no-timer` falls in the gap that leaves: the operator asked to run unsupervised, execution.md §8.3 requires that to *look* unsupervised, and with nothing recorded the run looks exactly like a hand-driven experiment nobody promised to schedule. So a launch writes itself down, and the item asks *did anyone launch this* as well as *did anyone arm it*.

Carries `tasks`, `definition`, and `timer` — the scheduler armed, or `None` where the launch was told not to arm one.
"""

JournalEvent = Event
"""One event in the journal."""

JournalRead = EventRead
"""Everything a journal file yielded, including what it could not."""


class InitializedEvent(JournalEvent):
    """The workspace was created.

    One of two typed events. Every other type in workflow.md's table arrives with the step that writes it, rather than being transcribed ahead of the code that gives it meaning.
    """

    definition: str | None = None
    """Definition filename the workspace expects, if it had one at `init`."""


class LaunchedEvent(JournalEvent):
    """A run was launched, and under what.

    Typed because a person reads this line: it is the top of the story every later `observation` continues, and the one place the journal says what the run is *of*.
    """

    definition: str
    """The definition captured, as the manifest records it — so a journal read months later names the file even if it has since moved."""

    tasks: int
    """How many tasks the committed manifest holds."""

    timer: str | None = None
    """Scheduler armed, or `None` where the launch was told not to arm one. What `read_launched` is folded for."""


@dataclass(frozen=True)
class Ack:
    """One disposal, as the fold reports it."""

    id: str
    by: str
    """`agent` or `human`. An agent disposing of a transient it investigated is its own ack, not a human's relayed through it."""

    reason: str
    ts: str


@dataclass(frozen=True)
class Collected:
    """How far an agent has read, and when it last did."""

    position: int
    """Journal line number read to."""

    ts: str
    """When, which is the other half of what this fold is for: the collection age sits beside the tend age so that *the timer stopped* and *nobody is looking* are distinguishable."""


@dataclass(frozen=True)
class Raised:
    """One hand-off, as the fold reports it."""

    id: str
    note: str
    """What the agent did to surface it, or empty. Optional by design — see `RAISED`."""

    ts: str


@dataclass(frozen=True)
class Paused:
    """The pause in force, as the fold reports it."""

    by: str
    """`agent` or `human`."""

    reason: str
    ts: str


@dataclass(frozen=True)
class Armed:
    """The timer in force, as the fold reports it."""

    scheduler: str
    """Which backend installed it: `launchd`, `systemd`, or `cron`."""

    interval: int
    """Seconds between tends, as the arming asked for."""

    label: str
    """The scheduler's name for this workspace's entry."""

    ts: str


@dataclass(frozen=True)
class JournalSummary:
    """What a journal says, at a glance."""

    count: int
    counts_by_type: dict[str, int]
    first_ts: str | None
    last_ts: str | None
    last: JournalEvent | None


def read_journal(journal: Path) -> JournalRead:
    """Read every event in a journal, reporting what could not be read.

    Args:
        journal: Path to `journal.jsonl`.

    Returns:
        The events, in file order, and one entry per line that was not one. A journal that does not exist is an empty history rather than damage.

    Raises:
        OSError: If the file exists but cannot be read. A missing journal is expected; an unreadable one is not.
    """
    return read_events(journal)


def read_acks(events: list[JournalEvent]) -> dict[str, Ack]:
    """Fold a journal down to what has been disposed of.

    An acknowledgment says a person or an agent looked at something nothing will clear mechanically and accepted it. The item then leaves every surface — `status.md`, the summary, the channel, the verdict — and this record is what it leaves behind (plan.md step 14).

    Keyed on the **item id**, which is chosen per kind so that a material change produces a different one. That is what makes a permanent-looking suppression safe: acknowledging a definition edit does not acknowledge the next edit, because the next edit hashes differently and is therefore a different item.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The most recent acknowledgment per item id. Later wins, so a re-acknowledgment carries the newer reason.
    """
    acks: dict[str, Ack] = {}
    for event in events:
        if event.type != ACKNOWLEDGED:
            continue
        identifier = event.payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            # a payload this version does not understand is data, not damage
            continue
        by = event.payload.get("by")
        reason = event.payload.get("reason")
        acks[identifier] = Ack(
            id=identifier,
            by=by if isinstance(by, str) else "",
            reason=reason if isinstance(reason, str) else "",
            ts=event.ts,
        )
    return acks


def read_raised(events: list[JournalEvent]) -> dict[str, Raised]:
    """Fold a journal down to what the agent has handed to its owner, and has not handed off twice.

    Keyed on the **item id** for the same reason `read_acks` is, and for most kinds that is the whole story: a stall raised at attempt 2 does not raise the one at attempt 3, because the attempt count is in the id.

    **A hand-off expires when a turn observes the item gone**, which the ids alone cannot express. Not every id encodes an instance — a park is keyed on its task, deliberately, so that one item stays stable while several samples in it wait rather than churning as each is answered. Without expiry that stability becomes silence: the first park is raised, somebody answers it, a second approval arrives in the same task hours later, and the id it re-uses is still marked handed-off, so `collect` sets aside a decision nobody has been told about. An observation records the ids that were open when it was written, so an id absent from one is a condition that ended — and a hand-off refers to *the episode it was made about*, which is over.

    An acknowledgment deliberately does **not** expire this way. The two acts differ in what they are about: raising says *I told somebody about this*, which stops being true of a condition that has been and gone, while acking says *this is accepted*, which stays true of the thing that was accepted however many times it recurs.

    The expiry is only as fine as the tend cadence, which is worth stating rather than hiding: a condition that appeared and cleared entirely between two turns was never observed to resolve, so a hand-off made inside that window survives it. What that costs is one un-repeated hand-off in the case where somebody is plainly present and answering.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The most recent hand-off per item id still open since it was made.
    """
    raised: dict[str, Raised] = {}
    for event in events:
        if event.type == OBSERVATION:
            open_ids = event.payload.get("items")
            # a payload with no item list is a turn this version cannot read
            # the open set from, not a turn that saw nothing -- expiring on it
            # would clear every hand-off in the file
            if isinstance(open_ids, list):
                still_open = {
                    entry
                    for entry in cast(list[object], open_ids)
                    if isinstance(entry, str)
                }
                raised = {
                    identifier: hand_off
                    for identifier, hand_off in raised.items()
                    if identifier in still_open
                }
            continue
        if event.type != RAISED:
            continue
        identifier = event.payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        note = event.payload.get("note")
        raised[identifier] = Raised(
            id=identifier,
            note=note if isinstance(note, str) else "",
            ts=event.ts,
        )
    return raised


def read_collected(events: list[JournalEvent]) -> Collected | None:
    """Fold a journal down to how far an agent has read.

    A switch rather than an accumulation, like the pause: the last word wins, so a `--since` that deliberately reaches backwards and then advances again leaves the newer position behind.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The most recent collection, or `None` where nobody has collected — which reads as *everything is new*, the right answer for a workspace no agent has attached to. Distinct from position `0`, which an agent could deliberately collect at.
    """
    collected: Collected | None = None
    for event in events:
        if event.type != COLLECTED:
            continue
        value = event.payload.get("position")
        if isinstance(value, int) and not isinstance(value, bool):
            collected = Collected(position=value, ts=event.ts)
    return collected


def read_pause(events: list[JournalEvent]) -> Paused | None:
    """Fold a journal down to whether the run is paused.

    A two-state fold rather than an accumulating one, so the last word wins and a double pause or a resume with nothing to resume is simply the state it leaves behind rather than an error somebody has to handle.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The pause in force, or `None` where the run is scheduling normally.
    """
    paused: Paused | None = None
    for event in events:
        if event.type == RESUMED:
            paused = None
        elif event.type == PAUSED:
            by = event.payload.get("by")
            reason = event.payload.get("reason")
            paused = Paused(
                by=by if isinstance(by, str) else "",
                reason=reason if isinstance(reason, str) else "",
                ts=event.ts,
            )
    return paused


def read_armed(events: list[JournalEvent]) -> Armed | None:
    """Fold a journal down to what timer is installed.

    The same two-state shape as `read_pause`. What it reports is what the *arming* said, not what the scheduler currently holds — nothing here shells out, because a turn runs this every ten minutes and `steward timer status` is where paying for the truth belongs.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The timer in force, or `None` where none was ever armed or the last word was a disarm.
    """
    armed: Armed | None = None
    for event in events:
        if event.type == DISARMED:
            armed = None
        elif event.type == ARMED:
            scheduler = event.payload.get("scheduler")
            interval = event.payload.get("interval")
            label = event.payload.get("label")
            if not isinstance(scheduler, str) or not isinstance(interval, int):
                # a payload this version does not understand is data, not damage
                continue
            armed = Armed(
                scheduler=scheduler,
                interval=interval,
                label=label if isinstance(label, str) else "",
                ts=event.ts,
            )
    return armed


def read_launched(events: list[JournalEvent]) -> str | None:
    """Fold a journal down to when this run was last launched.

    **Accumulating rather than two-state, because there is no un-launch.** `read_armed` and `read_pause` both have an event that undoes them, so the last word wins; a launch is a thing that happened, and the second one amends the first rather than cancelling it (workflow.md, *A second `launch` is the amend path*). So this only ever moves forward, and its answer to *was this run launched* is monotonic — which is the property `ever_launched` needs to be worth gating on.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        When the most recent launch was, or `None` where nothing ever launched this workspace.
    """
    launched: str | None = None
    for event in events:
        if event.type == LAUNCHED:
            launched = event.ts
    return launched


def summarize(events: list[JournalEvent]) -> JournalSummary:
    """Fold a journal down to what it says at a glance.

    The smallest useful fold, and the shape every later one takes: state is computed from the events on demand rather than maintained beside them.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        Counts, span, and the most recent event.
    """
    return JournalSummary(
        count=len(events),
        counts_by_type=dict(Counter(event.type for event in events)),
        first_ts=events[0].ts if events else None,
        last_ts=events[-1].ts if events else None,
        last=events[-1] if events else None,
    )
