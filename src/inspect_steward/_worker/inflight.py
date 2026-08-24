"""What was spawned, and which of it is still running.

Workers are detached, so there is no parent-child relationship to ask. Ground truth is the log directory — except for the window between a worker starting and its eval beginning, when it has written no log and bound no control socket, and so cannot be found by scanning either. A worker invisible in that window gets spawned a second time.

**The process table closes it, and is therefore the liveness source rather than a fallback.** Every worker's environment names its own selection document (`INSPECT_EVAL_SET_SELECTION` — the only marker every definition type can carry, since a frontend's CLI would reject an extra argument), so sweeping same-user processes for a selection path inside this workspace answers *what is running* from the instant a process exists. Measured at ~60ms for a table of 750 processes, which is affordable on every tend — and a scan that runs every time is exercised, where a recovery path that runs only after a crash is not.

**A worker's descendants carry the same marker, and are not the worker.** The environment is inherited, so a sandbox's `docker`, a frontend's `uv`, and every other subprocess an eval starts match the same selection path. Two rules separate them. The scan returns only the **ancestor-most** match of each subtree — a live worker's parent is the tend that spawned it, which carries no marker, so the worker is the unique root. And where the record knows a pid, that pid decides: a selection whose only surviving process is a leftover child reads as *departed*, because otherwise an orphan could hold a task open forever.

Matching on the path is what makes the pid safe to use rather than what replaces it. The question asked is *is the process we launched still running this selection*, and neither half answers it alone: a recycled pid fails the path test, and a descendant fails the pid test.

**The record is what the scan cannot know.** A worker whose `intent` was written and whose spawn never returned left no process to find, and must not block its task forever; a running worker's task identifier, attempt, and argv are provenance no process carries. So `.steward/inflight.jsonl` is appended before and after each spawn, and `resolve_inflight` reads the two together. Losing it costs provenance, and one degradation rather than correctness: an unrecorded worker still turns up in the scan with its identifier read from the selection document, but with no pid to check it against, an orphaned child of a dead worker reads as the worker.

See execution.md, *Detachment and the in-flight record*.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from socket import gethostname
from typing import Any

import psutil
from inspect_ai._control.discovery import list_discovered_servers
from inspect_ai._eval.eval_set_selection import (
    INSPECT_EVAL_SET_SELECTION,
    read_eval_set_selection,
)
from inspect_ai._util.error import PrerequisiteError

from .._schedule import DepartedWorker, InFlight, RunningWorker
from .._util.jsonl import Event, append_event, read_events

INTENT = "intent"
"""Written before the spawn: a worker that may exist."""

LAUNCHED = "launched"
"""Written after the spawn returns: a worker that did."""

EXITED = "exited"
"""Written when a worker is reaped, which is the only one of the three that is not this module's to write."""


@dataclass(frozen=True)
class ScannedWorker:
    """A live worker found in the process table.

    A worker rather than one of its subprocesses: the scan drops any match whose own parent matches too.
    """

    pid: int
    selection: Path
    """Resolved path to its selection document, which is both how it was found and how it is identified."""


WorkerScan = Callable[[Path], list[ScannedWorker]]
"""Find the workers running out of a workers directory. A parameter so the fold can be tested without processes."""


def record_intent(
    record: Path,
    *,
    worker: str,
    identifier: str,
    key: str,
    attempt: int,
    selection: Path,
    argv: list[str],
    cwd: str,
    log_dir: str,
) -> None:
    """Record that a worker is about to be spawned.

    Written **before** the spawn, which is the whole point of the record: a crash between here and the process existing leaves a worker whose existence nothing else would know about.

    Args:
        record: Path to `inflight.jsonl`.
        worker: Worker stem.
        identifier: Task identifier.
        key: Display key.
        attempt: 1-based attempt number.
        selection: Path to the worker's selection document.
        argv: The command being run.
        cwd: Working directory for the command.
        log_dir: Log directory the worker will write into.
    """
    _append(
        record,
        INTENT,
        worker=worker,
        identifier=identifier,
        key=key,
        attempt=attempt,
        selection=str(selection),
        argv=argv,
        cwd=cwd,
        log_dir=log_dir,
    )


def record_launched(record: Path, *, worker: str, pid: int) -> None:
    """Record that a worker's spawn returned.

    The process creation time is recorded alongside the pid as **provenance rather than as a liveness input**. It was originally there to defeat pid recycling; matching on the selection path defeats it more completely, so what this buys now is a diagnosable record — two entries claiming one pid are distinguishable after the fact.

    Args:
        record: Path to `inflight.jsonl`.
        worker: Worker stem.
        pid: Process id.
    """
    _append(record, LAUNCHED, worker=worker, pid=pid, started_at=_create_time(pid))


def record_exited(record: Path, *, worker: str, status: int | None = None) -> None:
    """Record that a worker is gone.

    Args:
        record: Path to `inflight.jsonl`.
        worker: Worker stem.
        status: Exit status, on the rare occasion there is one. Usually there is not: a worker outlives the tend that spawned it, so by the time anything observes it gone it has been reparented and reaped by init (execution.md, *Why `task -> pid` is not enough*).
    """
    _append(record, EXITED, worker=worker, status=status)


def resolve_inflight(
    record: Path,
    workers_dir: Path,
    *,
    host: str | None = None,
    scan: WorkerScan | None = None,
) -> InFlight:
    """Read the record and the process table, and sort workers into live and gone.

    Args:
        record: Path to `inflight.jsonl`. A missing one is an empty history, not an error — the scan alone still answers what is running.
        workers_dir: `.steward/workers/`, which bounds the scan. Another workspace's fleet on the same machine has its selections elsewhere, and is none of this one's business.
        host: This host (defaults to the current hostname).
        scan: How to find live workers (defaults to the process table).

    Returns:
        Workers confirmed alive, and workers the record accounts for that are not.
    """
    host = host or gethostname()
    scan = scan or scan_processes

    live: dict[Path, list[ScannedWorker]] = {}
    for found in scan(workers_dir):
        live.setdefault(found.selection, []).append(found)
    sockets = {server.pid: server.socket_path for server in list_discovered_servers()}

    running: list[RunningWorker] = []
    departed: list[DepartedWorker] = []
    for entry in _fold(read_events(record).events).values():
        # popped before the skips, deliberately: a selection the record knows
        # about belongs to that record, so a reaped worker's leftover children
        # cannot come back through the unrecorded branch below
        matched = live.pop(entry.selection, []) if entry.selection else []
        if entry.exited or entry.host != host:
            # a pid on another host means nothing here, and reaping a worker
            # this machine cannot see would be a claim rather than an observation
            continue
        found = _worker_process(matched, entry.pid)
        if found is not None:
            running.append(
                RunningWorker(
                    worker=entry.worker,
                    identifier=entry.identifier,
                    pid=found.pid,
                    host=host,
                    socket=sockets.get(found.pid),
                )
            )
        else:
            departed.append(
                DepartedWorker(
                    worker=entry.worker,
                    identifier=entry.identifier,
                    host=host,
                    pid=entry.pid,
                )
            )

    # whatever the scan found that the record does not account for. This is the
    # lost-record path and it needs no branch of its own: a worker's selection
    # document names the task it is running, so the scan alone is sufficient
    for selection, matched in live.items():
        identifier = _selected_identifier(selection)
        found = _worker_process(matched, None)
        if identifier is not None and found is not None:
            running.append(
                RunningWorker(
                    worker=selection.stem,
                    identifier=identifier,
                    pid=found.pid,
                    host=host,
                    socket=sockets.get(found.pid),
                )
            )

    return InFlight(running=running, departed=departed)


def _worker_process(
    matched: list[ScannedWorker], pid: int | None
) -> ScannedWorker | None:
    """Which of the processes running a selection document is the worker.

    Normally there is one, because the scan already dropped the descendants. More than one means the worker died and left children behind, which the recorded pid settles and nothing else can.
    """
    if pid is not None:
        # a recorded pid is the whole answer: if the process we launched is
        # gone, the worker is gone, whatever is still holding its selection
        return next((found for found in matched if found.pid == pid), None)
    return min(matched, key=lambda found: found.pid) if matched else None


def scan_processes(workers_dir: Path) -> list[ScannedWorker]:
    """Find every live worker running out of a workers directory.

    A worker's subprocesses inherit its environment and so match everything the marker can be searched for. They are excluded by ancestry rather than by any property of their own: a match whose parent also matches is somebody's child, and a worker's parent — the tend that spawned it — carries no marker at all. So each subtree contributes exactly its root, for as long as that root lives.

    Args:
        workers_dir: `.steward/workers/`. Only selections under it count.

    Returns:
        One entry per live worker, in no particular order.
    """
    root = workers_dir.resolve()
    matched: dict[int, tuple[int, Path]] = {}
    for process in psutil.process_iter():
        try:
            selection = process.environ().get(INSPECT_EVAL_SET_SELECTION)
            if not selection:
                continue
            path = Path(selection).resolve()
            if path.is_relative_to(root):
                matched[process.pid] = (process.ppid(), path)
        except psutil.Error:
            # gone between the listing and the read, a zombie, or another
            # user's. None of the three is a worker of ours.
            continue
    return [
        ScannedWorker(pid=pid, selection=path)
        for pid, (ppid, path) in matched.items()
        if ppid not in matched
    ]


@dataclass(frozen=True)
class _Recorded:
    """One worker's state, folded out of the record."""

    worker: str
    identifier: str
    host: str
    selection: Path | None = None
    pid: int | None = None
    exited: bool = False


def _fold(events: list[Event]) -> dict[str, _Recorded]:
    """Replay the record into the current state of each worker.

    Derived rather than stored, which is the same property the journal has and for the same reason: there is no separate state file to be inconsistent with the history that produced it.
    """
    workers: dict[str, _Recorded] = {}
    for event in events:
        payload = event.payload
        worker = payload.get("worker")
        if not isinstance(worker, str):
            continue
        if event.type == INTENT:
            identifier = payload.get("identifier")
            if not isinstance(identifier, str):
                continue
            workers[worker] = _Recorded(
                worker=worker,
                identifier=identifier,
                host=_str(payload.get("host")),
                selection=_path(payload.get("selection")),
            )
        elif (current := workers.get(worker)) is None:
            # a launched or exited with no intent before it: the record was
            # truncated or hand-edited, and there is nothing to attach it to
            continue
        elif event.type == LAUNCHED:
            workers[worker] = replace(current, pid=_int(payload.get("pid")))
        elif event.type == EXITED:
            workers[worker] = replace(current, exited=True)
    return workers


def _selected_identifier(selection: Path) -> str | None:
    """The task identifier a selection document names, or `None` if it cannot be read."""
    try:
        return read_eval_set_selection(str(selection)).tasks[0].identifier
    except PrerequisiteError:
        # every read failure arrives as this one, including a document a later
        # Steward wrote at a schema version this inspect refuses
        return None


def _append(record: Path, type: str, **fields: Any) -> None:
    """Append to the record, which is rebuildable and so is not flushed to disk."""
    record.parent.mkdir(parents=True, exist_ok=True)
    append_event(record, type, sync=False, host=gethostname(), **fields)


def _create_time(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        # a worker that died between the spawn returning and this call, which
        # a definition that raises on import does
        return None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _path(value: Any) -> Path | None:
    return Path(value).resolve() if isinstance(value, str) and value else None
