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
    "DamagedLine",
    "InitializedEvent",
    "JournalEvent",
    "JournalRead",
    "JournalSummary",
    "append_event",
    "read_journal",
    "summarize",
    "utc_now",
]

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
