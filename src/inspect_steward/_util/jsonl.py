"""Append-only JSONL, for the two records a workspace keeps as history.

The journal (`journal.jsonl`, decisions and observations) and the in-flight record (`.steward/inflight.jsonl`, what was spawned) are written by different subsystems for different reasons, and they want the same three properties for the same one: both are read *after* something went wrong, so both are shaped for the read that happens after a crash rather than for the ordinary one.

- **One record is one write.** Two processes append concurrently — a tend holds the run claim and an agent does not — so a record is built as a single string and written once. Splitting it across two writes (payload, then newline) loses about a quarter of the records under four concurrent writers, measured.
- **Damage costs one line, never the file.** A crash mid-append leaves a torn last line. It is reported and skipped; everything before it is intact.
- **An unrecognised type is data, not an error.** A workspace outlives the Steward that wrote it. This is the opposite of how a selection document is treated, and deliberately so: a selection is *input*, validated strictly before it changes what runs, while these are *history*.

Durability is where the two differ, which is why `append_event` takes `sync`. The journal is the one file in a workspace that nothing can rebuild; the in-flight record rebuilds from the process table on the next resolve.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

_MAX_DAMAGE_TEXT = 200


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


class Event(BaseModel):
    """One record in a JSONL history.

    Extra fields are kept rather than rejected: each type carries its own payload, and a type this version of Steward has never heard of still has to survive a round trip through a reader.
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


@dataclass(frozen=True)
class DamagedLine:
    """A line the reader could not turn into an event."""

    line: int
    """1-based line number."""

    reason: str
    text: str
    """The raw line, truncated, so a report can show what was there."""


@dataclass(frozen=True)
class EventRead:
    """Everything a JSONL file yielded, including what it could not."""

    events: list[Event]
    damage: list[DamagedLine]

    @property
    def intact(self) -> bool:
        return not self.damage


def append_event(path: Path, type: str, *, sync: bool = True, **fields: Any) -> None:
    """Append one event.

    Args:
        path: File to append to (created if absent).
        type: Event type.
        sync: Flush to disk before returning. Guards power loss rather than a process crash — nothing here tests it — and costs about a millisecond. Worth it for a record nothing can rebuild, and not for one the next resolve reconstructs.
        **fields: Event payload, merged into the envelope.

    Raises:
        OSError: If the file cannot be written. Never swallowed — for the journal, an event that did not land is a hole in the only copy.
    """
    line = json.dumps({"ts": utc_now(), "type": type, **fields}) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        if sync:
            os.fsync(fd)
    finally:
        os.close(fd)


def read_events(path: Path) -> EventRead:
    """Read every event in a JSONL file, reporting what could not be read.

    Never raises on damage, and never skips it silently: a torn last line is what a crash mid-append leaves behind, and the caller — not this function — decides whether that is worth complaining about, and to where (`steward.log`, not the record itself).

    Args:
        path: File to read.

    Returns:
        The events, in file order, and one entry per line that was not one. A file that does not exist is an empty history rather than damage.

    Raises:
        OSError: If the file exists but cannot be read. A missing file is expected; an unreadable one is not.
    """
    if not path.exists():
        return EventRead(events=[], damage=[])

    events: list[Event] = []
    damage: list[DamagedLine] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for number, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            if (failure := _parse(line, number, events)) is not None:
                damage.append(failure)

    return EventRead(events=events, damage=damage)


def _parse(line: str, number: int, into: list[Event]) -> DamagedLine | None:
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
    into.append(Event.model_validate(document))
    return None
