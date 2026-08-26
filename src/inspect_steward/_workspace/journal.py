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
    "Ack",
    "DamagedLine",
    "InitializedEvent",
    "JournalEvent",
    "JournalRead",
    "JournalSummary",
    "append_event",
    "read_acks",
    "read_journal",
    "summarize",
    "utc_now",
]

ACKNOWLEDGED = "acknowledged"
"""Journal event: somebody looked at an item and accepted it.

The one event kind an item list was not supposed to need. Items are a projection, so a condition that ends stops being reported and a decision keeps its subject open — but neither covers a real condition nothing will clear mechanically that somebody has already accepted. Without this, a definition edited on purpose reports drift every ten minutes for the rest of the run, which is how an attention list stops being read.

Written by `steward ack`, never by a tend. It carries `id`, `by` (`agent` or `human`), and a required `reason` — the discipline `inspect ctl` already imposes on every applied change, and what makes *who decided, and why* (workflow.md, *The audit trail*) true of this act too.
"""

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
