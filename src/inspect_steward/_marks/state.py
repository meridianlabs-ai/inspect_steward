"""The record of marking runs: what was started, and which of it is still running.

The same shape as the worker in-flight record (`_worker.inflight`), for the same reason: a runner is detached, so there is no parent to ask, and the window between a spawn and its first visible effect has to be closed by the process table. A run carries its own id in `STEWARD_MARK`, so the sweep answers *is it alive* and *what is it* from one read — and, as with a worker, the recorded pid decides: a zero's side workers inherit the variable, and a runner that died leaving one behind must read as gone.

**Spent attempts are the executor's to count, not this module's.** A finished run either journaled its application or did not, and only the journal says which (`Applied.runs`). This fold reports what ran and what is still running; the executor subtracts the runs that landed and bounds the rest.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from .._anomaly.model import Disposition
from .._util.jsonl import Event, append_event, read_events
from .._util.process import process_table
from .edit import Target

INTENT = "intent"
"""Written before the spawn: a run that may exist."""

LAUNCHED = "launched"
"""Written after the spawn returns: a run that did."""

EXITED = "exited"
"""Written by the runner itself on the way out, with its status."""

STEWARD_MARK = "STEWARD_MARK"
"""Environment variable naming the run a runner process carries out."""


@dataclass(frozen=True)
class MarkRun:
    """One run's state, folded out of the record."""

    run: str
    """Run id: the ruling's digest and the attempt number (`_marks.run.run_id`)."""

    class_key: str
    ruling_ts: str
    disposition: Disposition
    targets: tuple[Target, ...]
    """The ruled samples the run was to write, as the executor addressed them."""

    started: str
    """When its `intent` was written."""

    argv: tuple[str, ...] = ()
    pid: int | None = None
    exited: bool = False
    status: int | None = None
    """Exit status, where the runner got as far as recording one."""

    detail: str = ""
    """What the runner said on the way out — the failure, where it failed."""

    @property
    def key(self) -> tuple[str, str]:
        return (self.class_key, self.ruling_ts)


RunScan = Callable[[], Mapping[int, str]]
"""Find every live process carrying a run id, as `pid -> run`. A parameter so the fold can be tested without processes."""


@dataclass(frozen=True)
class Runs:
    """Every run the record holds, and which of them the process table confirms."""

    by_run: dict[str, MarkRun]
    live: frozenset[str] = frozenset()
    """Run ids whose recorded pid is alive and carrying that id."""

    def of(self, class_key: str, ruling_ts: str) -> list[MarkRun]:
        """This ruling's runs, in file order."""
        return [
            run for run in self.by_run.values() if run.key == (class_key, ruling_ts)
        ]

    def running(self, class_key: str, ruling_ts: str) -> MarkRun | None:
        """The run in progress for this ruling, if one is."""
        for run in self.of(class_key, ruling_ts):
            if run.run in self.live:
                return run
        return None

    def finished(self, class_key: str, ruling_ts: str) -> list[MarkRun]:
        """This ruling's runs that are no longer running, however they ended."""
        return [
            run for run in self.of(class_key, ruling_ts) if run.run not in self.live
        ]


def record_intent(
    record: Path,
    *,
    run: str,
    class_key: str,
    ruling_ts: str,
    disposition: Disposition,
    targets: Sequence[Target],
    argv: Sequence[str],
) -> None:
    """Record that a run is about to be spawned — before the spawn, which is the point of the record."""
    _append(
        record,
        INTENT,
        run=run,
        **{"class": class_key, "for": ruling_ts},
        disposition=disposition.value,
        targets=[target.as_record() for target in targets],
        argv=list(argv),
    )


def record_launched(record: Path, *, run: str, pid: int) -> None:
    """Record that the spawn returned."""
    _append(record, LAUNCHED, run=run, pid=pid)


def record_exited(record: Path, *, run: str, status: int, detail: str = "") -> None:
    """Record how a run ended. The runner's own last act."""
    _append(record, EXITED, run=run, status=status, detail=detail)


def read_runs(record: Path) -> dict[str, MarkRun]:
    """Replay the record into the current state of each run.

    Derived rather than stored, on the discipline every record here keeps. A line this version cannot read is skipped: the record is machine-written and disposable, and a torn line costs one run's provenance rather than the fold.
    """
    return _fold(read_events(record).events)


def resolve_runs(record: Path, *, scan: RunScan | None = None) -> Runs:
    """Read the record and the process table, and say which runs are still going.

    Args:
        record: Path to `runs.jsonl`. A missing one is an empty history.
        scan: How to find live runners (defaults to the process table).

    Returns:
        Every recorded run, with the live ones named.
    """
    by_run = read_runs(record)
    if not by_run:
        return Runs(by_run={})
    found = (scan or scan_runs)()
    # a run that recorded its own exit is finished whatever the table says:
    # its pid may since have been reused, and the side workers it left
    # behind carry the same id
    live = frozenset(
        run.run
        for run in by_run.values()
        if run.pid is not None and not run.exited and found.get(run.pid) == run.run
    )
    return Runs(by_run=by_run, live=live)


def scan_runs() -> dict[int, str]:
    """Every live process carrying a run id, by pid.

    A runner's side workers inherit the variable and are not the runner; the recorded pid is what separates them, which is why `resolve_runs` matches on both.
    """
    return {
        found.pid: run
        for found in process_table()
        if (run := found.environ.get(STEWARD_MARK))
    }


def _fold(events: list[Event]) -> dict[str, MarkRun]:
    runs: dict[str, MarkRun] = {}
    for event in events:
        payload = event.payload
        run = payload.get("run")
        if not isinstance(run, str) or not run:
            continue
        if event.type == INTENT:
            folded = _intent(run, event.ts, payload)
            if folded is not None:
                runs[run] = folded
        elif (current := runs.get(run)) is None:
            continue
        elif event.type == LAUNCHED:
            runs[run] = replace(current, pid=_int(payload.get("pid")))
        elif event.type == EXITED:
            detail = payload.get("detail")
            runs[run] = replace(
                current,
                exited=True,
                status=_int(payload.get("status")),
                detail=detail if isinstance(detail, str) else "",
            )
    return runs


def _intent(run: str, ts: str, payload: dict[str, Any]) -> MarkRun | None:
    class_key = payload.get("class")
    ruling_ts = payload.get("for")
    try:
        disposition = Disposition(payload.get("disposition"))
    except ValueError:
        return None
    if not (
        isinstance(class_key, str)
        and class_key
        and isinstance(ruling_ts, str)
        and ruling_ts
    ):
        return None
    recorded = payload.get("targets")
    targets = tuple(
        target
        for entry in (
            cast(list[object], recorded) if isinstance(recorded, list) else []
        )
        if (target := Target.from_record(entry)) is not None
    )
    argv = payload.get("argv")
    return MarkRun(
        run=run,
        class_key=class_key,
        ruling_ts=ruling_ts,
        disposition=disposition,
        targets=targets,
        started=ts,
        argv=tuple(
            entry
            for entry in (cast(list[object], argv) if isinstance(argv, list) else [])
            if isinstance(entry, str)
        ),
    )


def _append(record: Path, type: str, **fields: Any) -> None:
    record.parent.mkdir(parents=True, exist_ok=True)
    append_event(record, type, sync=False, **fields)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = [
    "EXITED",
    "INTENT",
    "LAUNCHED",
    "STEWARD_MARK",
    "MarkRun",
    "RunScan",
    "Runs",
    "read_runs",
    "record_exited",
    "record_intent",
    "record_launched",
    "resolve_runs",
    "scan_runs",
]
