"""The journal: the one record in a workspace that nothing can rebuild.

A manifest re-captures from the definition, anomalies re-derive from the log directory, in-flight records rebuild from the logs — but a ruling and its reasoning exist nowhere else. So this module is written for two situations rather than one: the ordinary append, and the read that happens after something went wrong.

Two properties follow from that, and both are the opposite of how Steward treats a selection document:

- **Damage costs one line, never the file.** A crash mid-append leaves a torn last line. It is reported and skipped; everything before it is intact.
- **An unrecognised event type is data, not an error.** A workspace outlives the Steward that wrote it. A selection document is *input*, validated strictly before it changes what runs; a journal is *history*, and refusing to read history because a later version wrote something new would be the wrong trade entirely.

State is derived from this file rather than stored beside it (workflow.md, *State is a fold over the journal*), which is what makes crash recovery the normal code path instead of a rescue routine nobody tests.
"""

import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict


def utc_now() -> str:
    """Current time as a UTC ISO-8601 string.

    Every instant Steward records is UTC with an explicit offset — a workspace is read on a different machine than it was written on often enough that a naive local timestamp is a latent bug (execution.md, *Clocks*).

    Returns:
        Timestamp, e.g. `2026-08-23T18:44:02.913Z`.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class JournalEvent(BaseModel):
    """One event in the journal.

    Extra fields are kept rather than rejected: each event type carries its own payload, and a type this version of Steward has never heard of still has to survive a round trip through a reader.
    """

    model_config = ConfigDict(extra="allow")

    ts: str
    """When it happened (UTC ISO-8601)."""

    type: str
    """Event type."""

    @property
    def payload(self) -> dict[str, Any]:
        """Fields beyond the envelope."""
        return self.__pydantic_extra__ or {}


class InitializedEvent(JournalEvent):
    """The workspace was created.

    The only typed event so far. Every other type in workflow.md's table arrives with the step that writes it, rather than being transcribed ahead of the code that gives it meaning.
    """

    definition: str | None = None
    """Definition filename the workspace expects, if it had one at `init`."""


@dataclass(frozen=True)
class DamagedLine:
    """A line the reader could not turn into an event."""

    line: int
    """1-based line number."""

    reason: str
    text: str
    """The raw line, truncated, so a report can show what was there."""


@dataclass(frozen=True)
class JournalRead:
    """Everything a journal file yielded, including what it could not."""

    events: list[JournalEvent]
    damage: list[DamagedLine]

    @property
    def intact(self) -> bool:
        return not self.damage


@dataclass(frozen=True)
class JournalSummary:
    """What a journal says, at a glance."""

    count: int
    counts_by_type: dict[str, int]
    first_ts: str | None
    last_ts: str | None
    last: JournalEvent | None


_MAX_DAMAGE_TEXT = 200


def append_event(journal: Path, type: str, **fields: Any) -> None:
    """Append one event to the journal.

    **Two processes append here** — a tend holds the run claim, and an agent recording a proposal or a collection does not — so the record is built as one string and written once. That single write is the whole guarantee, and it is the part worth protecting: splitting a record across two writes (payload, then newline) loses about a quarter of the events under four concurrent writers, measured. Size is not the hazard and neither is the platform; an append-mode write of a whole line does not interleave on a local filesystem, which is the topology a workspace runs on.

    Flushed to disk before returning. That guards power loss rather than a process crash — nothing here tests it — and costs a millisecond sixty times a night against the one file that cannot be rebuilt.

    Args:
        journal: Path to `journal.jsonl`.
        type: Event type.
        **fields: Event payload, merged into the envelope.

    Raises:
        OSError: If the journal cannot be written. Never swallowed — an event that did not land is a hole in the only copy.
    """
    line = json.dumps({"ts": utc_now(), "type": type, **fields}) + "\n"
    fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_journal(journal: Path) -> JournalRead:
    """Read every event in a journal, reporting what could not be read.

    Never raises on damage, and never skips it silently: a torn last line is what a crash mid-append leaves behind, and the caller — not this function — decides whether that is worth complaining about, and to where (`steward.log`, not the journal itself).

    Args:
        journal: Path to `journal.jsonl`.

    Returns:
        The events, in file order, and one entry per line that was not one. A journal that does not exist is an empty history rather than damage.

    Raises:
        OSError: If the file exists but cannot be read. A missing journal is expected; an unreadable one is not.
    """
    if not journal.exists():
        return JournalRead(events=[], damage=[])

    events: list[JournalEvent] = []
    damage: list[DamagedLine] = []
    with journal.open("r", encoding="utf-8", errors="replace") as f:
        for number, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            if (failure := _parse(line, number, events)) is not None:
                damage.append(failure)

    return JournalRead(events=events, damage=damage)


def _parse(line: str, number: int, into: list[JournalEvent]) -> DamagedLine | None:
    """Parse one line, appending the event or describing why it is not one."""

    def damaged(reason: str) -> DamagedLine:
        return DamagedLine(line=number, reason=reason, text=line[:_MAX_DAMAGE_TEXT])

    try:
        parsed: Any = json.loads(line)
    except ValueError as ex:
        # the expected case: a process died between the write starting and the
        # newline landing, so the tail is a fragment of valid JSON
        return damaged(f"not valid JSON ({ex})")

    if not isinstance(parsed, dict):
        return damaged("not a JSON object")

    document = cast(dict[str, Any], parsed)
    ts, type = document.get("ts"), document.get("type")
    if not isinstance(ts, str) or not isinstance(type, str):
        return damaged("missing a string 'ts' or 'type'")

    # deliberately not dispatched to a typed subclass: a reader's job is to
    # yield history, and an event whose payload a later version changed is
    # still history. Callers that need a typed view validate what they asked for.
    into.append(JournalEvent.model_validate(document))
    return None


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
