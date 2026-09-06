"""What the last turn harvested from each running task's interim scoring pass.

A completed-only pass is free of holds and grader calls, but it is still a request per running task per turn, and most turns it would return the same numbers: nothing new has been scored. So a turn keeps what it harvested, keyed by eval id and stamped with how many scored samples the figures describe, and asks again only when the worker's sample listing says that count grew. The file is disposable exactly like `classed.json`: losing it costs one pass per running task, never a wrong number.

**A tend writes it and a status does not**, on the discipline every cache under `.steward/` keeps. A status that finds its gate open runs the pass and shows the figure; the next tend runs it once more and records it.
"""

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from .live import Interim, InterimEntry

INTERIM_VERSION = 1
"""Bumped when `Interim` changes shape. A file written by another version is discarded rather than migrated."""


def read_interim(path: Path) -> dict[str, Interim]:
    """Read the harvest, treating every failure as an empty one.

    Args:
        path: `.steward/interim.json`.

    Returns:
        What the last tend harvested, by eval id; empty when absent, unreadable, or from another version.
    """
    try:
        loaded: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    document = cast(dict[str, object], loaded)
    if document.get("version") != INTERIM_VERSION:
        return {}
    evals = document.get("evals")
    if not isinstance(evals, dict):
        return {}
    known: dict[str, Interim] = {}
    for eval_id, raw in cast(dict[str, object], evals).items():
        interim = _interim(raw)
        if interim is not None:
            known[eval_id] = interim
    return known


def _interim(raw: object) -> Interim | None:
    if not isinstance(raw, dict):
        return None
    record = cast(dict[str, object], raw)
    scored = record.get("scored")
    if not isinstance(scored, int) or isinstance(scored, bool):
        return None
    entries = record.get("entries")
    if not isinstance(entries, list):
        return None
    return Interim(
        scored=scored,
        entries=tuple(
            entry
            for raw_entry in cast(list[object], entries)
            if (entry := _entry(raw_entry)) is not None
        ),
    )


def _entry(raw: object) -> InterimEntry | None:
    if not isinstance(raw, dict):
        return None
    record = cast(dict[str, object], raw)
    name = record.get("name")
    reducer = record.get("reducer")
    metrics = record.get("metrics")
    if not isinstance(name, str) or not isinstance(metrics, dict):
        return None
    return InterimEntry(
        name=name,
        reducer=reducer if isinstance(reducer, str) else None,
        metrics={
            key: float(value)
            for key, value in cast(dict[str, object], metrics).items()
            if isinstance(value, int | float) and not isinstance(value, bool)
        },
    )


def write_interim(path: Path, known: Mapping[str, Interim]) -> None:
    """Write the harvest, and never at the cost of the turn.

    Args:
        path: `.steward/interim.json`.
        known: What this turn holds, by eval id, already narrowed to what is still running.
    """
    document: dict[str, Any] = {
        "version": INTERIM_VERSION,
        "evals": {
            eval_id: {
                "scored": interim.scored,
                "entries": [
                    {
                        "name": entry.name,
                        "reducer": entry.reducer,
                        "metrics": dict(entry.metrics),
                    }
                    for entry in interim.entries
                ],
            }
            for eval_id, interim in known.items()
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
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError:
        # disposable: the next tend harvests again
        return
