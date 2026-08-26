"""The journal: the one record in a workspace that nothing can rebuild.

A manifest re-captures from the definition, anomalies re-derive from the log directory, the in-flight record rebuilds from the process table — but a ruling and its reasoning exist nowhere else. So the append here is flushed to disk, and the read is written for the situation after a crash rather than the ordinary one.

The mechanics live in `_util.jsonl`, which the in-flight record shares: single-write appends, damage costing one line, and an unrecognised type reading as data rather than as an error. What is journal-specific is the durability (`sync=True`, the default) and the vocabulary below.

State is derived from this file rather than stored beside it (workflow.md, *State is a fold over the journal*), which is what makes crash recovery the normal code path instead of a rescue routine nobody tests.
"""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

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
    "DISARMED",
    "PAUSED",
    "RESUMED",
    "Ack",
    "Armed",
    "DamagedLine",
    "InitializedEvent",
    "JournalEvent",
    "JournalRead",
    "JournalSummary",
    "Paused",
    "append_event",
    "read_acks",
    "read_armed",
    "read_journal",
    "read_pause",
    "summarize",
    "utc_now",
]

ACKNOWLEDGED = "acknowledged"
"""Journal event: somebody looked at an item and accepted it.

The one event kind an item list was not supposed to need. Items are a projection, so a condition that ends stops being reported and a decision keeps its subject open — but neither covers a real condition nothing will clear mechanically that somebody has already accepted. Without this, a definition edited on purpose reports drift every ten minutes for the rest of the run, which is how an attention list stops being read.

Written by `steward ack`, never by a tend. It carries `id`, `by` (`agent` or `human`), and a required `reason` — the discipline `inspect ctl` already imposes on every applied change, and what makes *who decided, and why* (workflow.md, *The audit trail*) true of this act too.
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

JournalEvent = Event
"""One event in the journal."""

JournalRead = EventRead
"""Everything a journal file yielded, including what it could not."""


class InitializedEvent(JournalEvent):
    """The workspace was created.

    The only typed event so far. Every other type in workflow.md's table arrives with the step that writes it, rather than being transcribed ahead of the code that gives it meaning.
    """

    definition: str | None = None
    """Definition filename the workspace expects, if it had one at `init`."""


@dataclass(frozen=True)
class Ack:
    """One disposal, as the fold reports it."""

    id: str
    by: str
    """`agent` or `human`. An agent disposing of a transient it investigated is its own ack, not a human's relayed through it."""

    reason: str
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
