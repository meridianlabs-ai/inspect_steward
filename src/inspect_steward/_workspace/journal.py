"""The journal's envelope and append path.

Deliberately minimal: `init` needs to write one event, and the rest of the journal — the fold, the event vocabulary, tolerance of a torn last line — belongs to the step that builds state out of it. What is fixed here is the shape every event shares, because `init` writing the first record means the format is decided now whether or not the reader is.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    """Current time as a UTC ISO-8601 string.

    Every timestamp Steward writes is UTC, so that a run spanning a timezone change, or a workspace read on another machine, orders correctly.

    Returns:
        Timestamp, e.g. `2026-08-23T18:44:02.913Z`.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def append_event(journal: Path, type: str, **fields: Any) -> None:
    """Append one event to the journal.

    Args:
        journal: Path to `journal.jsonl`.
        type: Event type.
        **fields: Event payload, merged into the envelope.

    Raises:
        OSError: If the journal cannot be written. The journal is the one record that cannot be rebuilt, so a failed append is never swallowed.
    """
    event = {"ts": utc_now(), "type": type, **fields}
    # one open per append, in append mode: a write that loses the race with
    # another appender still lands whole, because a single write() under the
    # pipe-buffer size is atomic on POSIX and every event is far below it
    with journal.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
