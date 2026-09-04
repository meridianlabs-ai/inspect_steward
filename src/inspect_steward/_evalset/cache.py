"""Header reads a turn does not have to do again.

Observation reads the header of every log in the directory, every turn. That is right the first time and waste every time after: a finished log is never rewritten, so on the fiftieth tend of a campaign Steward re-reads two thousand headers to learn what forty-nine previous tends already knew. The cost is linear in the campaign and paid on a timer, which is the shape that degrades exactly when a run gets interesting.

**The listing already carries the key, which is what makes this nearly free.** `list_eval_logs` returns `mtime` and `size` per file for about 0.011ms each and no file opens at all, against 1.4ms and two opens for a header read of an `.eval`. So a turn can decide what it needs to re-read before reading anything, and a directory of two thousand settled logs costs the listing rather than the reads.

**Keyed on modification time and size together, which is exactly the mutation signal.** A finished log is immutable, so it hits. A running log grows, so it misses and is re-read. An *invalidated* log is rewritten by the operator who invalidated it, so its mtime moves and it is re-read — the same signal `_stalled` reads to date an invalidation, so the two cannot disagree about when it happened.

**A `started` log is never cached at all.** It is the one file that changes constantly, and the one where a same-second rewrite that happened to preserve the size would go unnoticed. Excluding it costs nothing — a running fleet is a handful of files against a directory of thousands — and removes the only case where the key is not conclusive.

The file is disposable and rebuildable, like `steward.log` and `status.md`: losing it costs one slow turn. So nothing here raises. A cache that cannot be read is an empty one, and a cache that cannot be written is a turn that was still correct.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from inspect_ai.log import EvalLogInfo

from .observe import LogAttempt

CACHE_VERSION = 4
"""Bumped when `LogAttempt` changes shape. A cache written by another version is discarded rather than migrated — it is derived data, and rebuilding it costs one turn."""

RUNNING = "started"
"""The one status never cached. See the module docstring."""


@dataclass(frozen=True)
class _Entry:
    mtime: float | None
    size: int | None
    attempt: LogAttempt


@dataclass
class AttemptCache:
    """What previous turns learned about logs that have not changed since.

    Mutable, deliberately: a turn reads it, fills it as it goes, and writes back what the current listing still names — which is also what prunes it, since an archived log simply stops being offered.
    """

    entries: dict[str, _Entry] = field(default_factory=dict[str, _Entry])

    hits: int = 0
    misses: int = 0

    def get(self, info: EvalLogInfo) -> LogAttempt | None:
        """The attempt already known for this file, if it is still the same file.

        Args:
            info: The listing's entry for one log.

        Returns:
            The attempt, or `None` when it must be read.
        """
        entry = self.entries.get(info.name)
        if entry is None or entry.mtime != info.mtime or entry.size != info.size:
            self.misses += 1
            return None
        self.hits += 1
        return entry.attempt

    def put(self, info: EvalLogInfo, attempt: LogAttempt) -> None:
        """Remember an attempt, unless it is one that will change again.

        Args:
            info: The listing's entry for the log.
            attempt: What reading its header produced.
        """
        if attempt.status == RUNNING:
            return
        self.entries[info.name] = _Entry(
            mtime=info.mtime, size=info.size, attempt=attempt
        )

    def keep(self, locations: set[str]) -> "AttemptCache":
        """This cache narrowed to the files a listing still names.

        Which is how it stays bounded without a policy: an archived log leaves the directory, stops being offered, and its entry goes with it.

        Args:
            locations: Locations the current listing returned.

        Returns:
            A cache holding only those.
        """
        return AttemptCache(
            entries={
                location: entry
                for location, entry in self.entries.items()
                if location in locations
            },
            hits=self.hits,
            misses=self.misses,
        )


def read_attempt_cache(path: Path) -> AttemptCache:
    """Read the cache, treating every failure as an empty one.

    Args:
        path: `.steward/observed.json`.

    Returns:
        What it held, or an empty cache when it is absent, unreadable, or from another version.
    """
    try:
        loaded: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return AttemptCache()

    if not isinstance(loaded, dict):
        return AttemptCache()
    document = cast(dict[str, object], loaded)
    if document.get("version") != CACHE_VERSION:
        return AttemptCache()

    logs = document.get("logs")
    if not isinstance(logs, dict):
        return AttemptCache()

    entries: dict[str, _Entry] = {}
    for location, raw in cast(dict[str, object], logs).items():
        if not isinstance(raw, dict):
            continue
        record = cast(dict[str, object], raw)
        mtime, size, attempt = (
            record.get("mtime"),
            record.get("size"),
            record.get("attempt"),
        )
        if not isinstance(attempt, dict):
            continue
        try:
            entries[location] = _Entry(
                mtime=mtime if isinstance(mtime, float | int) else None,
                size=size if isinstance(size, int) else None,
                attempt=LogAttempt(**cast(dict[str, Any], attempt)),
            )
        except TypeError:
            # a record written by a version whose `LogAttempt` had other fields.
            # One damaged entry costs one header read, not the whole cache
            continue
    return AttemptCache(entries=entries)


def write_attempt_cache(path: Path, cache: AttemptCache) -> None:
    """Write the cache, and never at the cost of the turn.

    Args:
        path: `.steward/observed.json`.
        cache: What this turn learned.
    """
    document: dict[str, Any] = {
        "version": CACHE_VERSION,
        "logs": {
            location: {
                "mtime": entry.mtime,
                "size": entry.size,
                "attempt": asdict(entry.attempt),
            }
            for location, entry in cache.entries.items()
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError:
        return
