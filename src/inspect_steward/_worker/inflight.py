"""What was spawned, and which of it is still running.

Workers are detached, so there is no parent-child relationship to ask. Ground truth is the log directory — except for the window between a worker starting and its eval beginning, when it has written no log and bound no control socket, and so cannot be found by scanning either. A worker invisible in that window gets spawned a second time.

**The process table closes it, and is therefore the liveness source rather than a fallback.** Every worker's environment names its own selection document (`INSPECT_EVAL_SET_SELECTION` — the only marker every definition type can carry, since a frontend's CLI would reject an extra argument), so sweeping same-user processes for a selection path inside this workspace answers *what is running* from the instant a process exists. Measured at ~60ms for a table of 750 processes, which is affordable on every tend — and a scan that runs every time is exercised, where a recovery path that runs only after a crash is not.

**A worker carries its identity there too**, in `STEWARD_WORKER` and `STEWARD_TASK`, so the sweep answers *what is this* from the same read that answered *is it alive*. Taking the identifier from the selection document instead would work — it names one — but it makes identity depend on a file, and `.steward/` is a directory the design tells people they may delete. It cost one line of environment to stop a deletion mid-run from hiding a live worker and getting it respawned over itself.

**A worker's descendants carry the same marker, and are not the worker.** The environment is inherited, so a sandbox's `docker`, a frontend's `uv`, and every other subprocess an eval starts match the same selection path. Two rules separate them. The scan returns only the **ancestor-most** match of each subtree — a live worker's parent is the tend that spawned it, which carries no marker, so the worker is the unique root. And where the record knows a pid, that pid decides: a selection whose only surviving process is a leftover child reads as *departed*, because otherwise an orphan could hold a task open forever.

Matching on the path is what makes the pid safe to use rather than what replaces it. The question asked is *is the process we launched still running this selection*, and neither half answers it alone: a recycled pid fails the path test, and a descendant fails the pid test.

**The record is what the scan cannot know.** A worker whose `intent` was written and whose spawn never returned left no process to find, and must not block its task forever; a running worker's attempt, key, and argv are provenance no process carries. So `.steward/inflight.jsonl` is appended before and after each spawn, and `resolve_inflight` reads the two together. Losing it costs that provenance, and one degradation rather than correctness: an unrecorded worker still turns up in the scan naming its own task, but with no recorded pid to check it against, an orphaned child of a dead worker reads as the worker.

See execution.md, *Detachment and the in-flight record*.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from socket import gethostname
from typing import Any, cast

import psutil
from inspect_ai._control.discovery import list_discovered_servers
from inspect_ai._eval.eval_set_selection import INSPECT_EVAL_SET_SELECTION

from .._schedule import DepartedWorker, InFlight, RunningWorker, SpawnTask
from .._util.jsonl import Event, append_event, read_events
from .._util.process import process_table

INTENT = "intent"
"""Written before the spawn: a worker that may exist."""

LAUNCHED = "launched"
"""Written after the spawn returns: a worker that did."""

EXITED = "exited"
"""Written when a worker is reaped, which is the only one of the three that is not this module's to write."""

STEWARD_WORKER = "STEWARD_WORKER"
"""Environment variable naming a worker's stem. Steward's own, alongside inspect's selection marker."""

STEWARD_TASK = "STEWARD_TASK"
"""Environment variable naming the task identifiers a worker is running, one per line.

Here rather than read from the selection document, so that the scan answers *what is this* from the same place it answers *is it alive*. The document would do — it names them and the worker was launched with its path — but reading it makes identity depend on a file, and `.steward/` is a directory the design tells people they may delete.

A list rather than a second variable for the packed case: a worker running one task exports exactly the value it did before packing existed, so there is one variable to read and no way for a reader to consult the wrong one."""


@dataclass(frozen=True)
class ScannedWorker:
    """A live worker found in the process table.

    A worker rather than one of its subprocesses: the scan drops any match whose own parent matches too.
    """

    pid: int
    selection: Path
    """Resolved path to its selection document, which is how it was found and how it is scoped to this workspace."""

    worker: str | None = None
    """File stem, from the environment. `None` for a marked process Steward did not spawn."""

    identifiers: tuple[str, ...] = ()
    """Task identifiers, from the environment. Empty for a marked process Steward did not spawn."""


WorkerScan = Callable[[Path], list[ScannedWorker]]
"""Find the workers running out of a workers directory. A parameter so the fold can be tested without processes."""


def record_intent(
    record: Path,
    *,
    worker: str,
    tasks: Sequence[SpawnTask],
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
        tasks: What this worker will run. Each carries its own identifier, key, and attempt number, because all three are facts about the task rather than the process.
        selection: Path to the worker's selection document.
        argv: The command being run.
        cwd: Working directory for the command.
        log_dir: Log directory the worker will write into.
    """
    _append(
        record,
        INTENT,
        worker=worker,
        tasks=[
            dict(identifier=task.identifier, key=task.key, attempt=task.attempt)
            for task in tasks
        ],
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
        Workers confirmed alive, workers the record accounts for that are not, and when each task's already-spent attempts began.
    """
    host = host or gethostname()
    scan = scan or scan_processes

    live: dict[Path, list[ScannedWorker]] = {}
    for found in scan(workers_dir):
        live.setdefault(found.selection, []).append(found)
    sockets = {server.pid: server.socket_path for server in list_discovered_servers()}

    running: list[RunningWorker] = []
    departed: list[DepartedWorker] = []
    spent: dict[str, list[str]] = {}
    for entry in _fold(read_events(record).events).values():
        # popped before the skips, deliberately: a selection the record knows
        # about belongs to that record, so a reaped worker's leftover children
        # cannot come back through the unrecorded branch below
        matched = live.pop(entry.selection, []) if entry.selection else []
        if entry.host != host:
            # a pid on another host means nothing here, and reaping a worker
            # this machine cannot see would be a claim rather than an observation
            continue
        if entry.exited:
            # already reaped, so there is nothing to report -- but it was an
            # attempt, and recording it is the only trace a worker that died
            # before landing a log leaves anywhere. One per task it held: a
            # packed worker that died spent an attempt on every one of them
            for identifier in entry.identifiers:
                spent.setdefault(identifier, []).append(entry.started)
            continue
        found = _worker_process(matched, entry.pid)
        if found is not None:
            running.append(
                RunningWorker(
                    worker=entry.worker,
                    identifiers=entry.identifiers,
                    pid=found.pid,
                    host=host,
                    socket=sockets.get(found.pid),
                )
            )
        else:
            departed.append(
                DepartedWorker(
                    worker=entry.worker,
                    identifiers=entry.identifiers,
                    host=host,
                    pid=entry.pid,
                    started=entry.started,
                )
            )
            for identifier in entry.identifiers:
                spent.setdefault(identifier, []).append(entry.started)

    # whatever the scan found that the record does not account for. This is the
    # lost-record path and it needs no branch of its own: a worker carries its
    # own identity in its environment, so the scan alone is sufficient
    for selection, matched in live.items():
        found = _worker_process(matched, None)
        if found is not None and found.identifiers:
            running.append(
                RunningWorker(
                    worker=found.worker or selection.stem,
                    identifiers=found.identifiers,
                    pid=found.pid,
                    host=host,
                    socket=sockets.get(found.pid),
                )
            )

    return InFlight(running=running, departed=departed, spent=spent)


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
    matched: dict[int, tuple[int, ScannedWorker]] = {}
    # a process that could not be read is already skipped: gone between the
    # listing and the read, a zombie, or another user's, and none of the three
    # is a worker of ours (`_util.process`)
    for found in process_table():
        selection = found.environ.get(INSPECT_EVAL_SET_SELECTION)
        if not selection:
            continue
        path = Path(selection).resolve()
        if path.is_relative_to(root):
            matched[found.pid] = (
                found.ppid,
                ScannedWorker(
                    pid=found.pid,
                    selection=path,
                    worker=found.environ.get(STEWARD_WORKER),
                    identifiers=_identifiers(found.environ.get(STEWARD_TASK)),
                ),
            )
    return [found for ppid, found in matched.values() if ppid not in matched]


@dataclass(frozen=True)
class _Recorded:
    """One worker's state, folded out of the record."""

    worker: str
    identifiers: tuple[str, ...]
    host: str
    started: str
    """When its `intent` was written, which is the closest thing the record has to when the attempt began. Kept because an attempt that landed no log can only be placed in a task's history by its time."""

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
            identifiers = _recorded_identifiers(payload.get("tasks"))
            if not identifiers:
                continue
            workers[worker] = _Recorded(
                worker=worker,
                identifiers=identifiers,
                host=_str(payload.get("host")),
                started=event.ts,
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


def _identifiers(value: str | None) -> tuple[str, ...]:
    """Split `STEWARD_TASK` into the tasks a worker is running, one per line."""
    return tuple(line for line in (value or "").splitlines() if line)


def _recorded_identifiers(value: Any) -> tuple[str, ...]:
    """The identifiers an `intent` names, skipping anything malformed.

    Lenient in the same way the rest of this fold is: the record is machine-written and rebuildable, so a line that cannot be read is dropped rather than raised on. An `intent` left with no readable task is discarded by the caller, which is the same answer it already gave to one naming no task at all.
    """
    if not isinstance(value, list):
        return ()
    identifiers: list[str] = []
    for task in cast(list[Any], value):
        if not isinstance(task, dict):
            continue
        identifier = cast(dict[str, Any], task).get("identifier")
        if isinstance(identifier, str) and identifier:
            identifiers.append(identifier)
    return tuple(identifiers)


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
