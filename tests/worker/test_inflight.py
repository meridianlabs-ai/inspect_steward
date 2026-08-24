"""What was spawned, and which of it is still running.

Most of this is a table, because the scan is a parameter: given a record and a
set of live selection paths, `resolve_inflight` is a pure fold. What the table
cannot claim is anything about real processes — that the scan finds one during
the window where nothing else can, and that it can tell a worker from the
subprocesses that inherited its marker. Three eval workers are launched for the
first; the second is four `python -c` processes, which cost nothing.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from socket import gethostname
from typing import Any, TypeAlias

import pytest
from inspect_ai._eval.eval_set_selection import INSPECT_EVAL_SET_SELECTION
from inspect_ai.log import list_eval_logs
from inspect_steward import read_eval_set
from inspect_steward._evalset.observe import observe_logs, observe_tasks
from inspect_steward._schedule import Pool, ReapWorker, SpawnWorker, reconcile
from inspect_steward._util.jsonl import read_events, utc_now
from inspect_steward._worker import (
    INTENT,
    Fleet,
    ScannedWorker,
    SpawnedWorker,
    WorkerScan,
    record_exited,
    record_intent,
    record_launched,
    resolve_inflight,
    scan_processes,
    worker_selection,
)

from ._fleet import EVAL_SET_ID, FIXTURES, action, fleet, output

HERE = gethostname()
"""This host. The real name rather than a placeholder, because the writers stamp it themselves — a table that made one up would be testing its own constant."""

ELSEWHERE = "another-host"

POOL = Pool(max_workers=8)

Write = Callable[[Path, Path], None]
"""One line into the record, given the record and the workers directory."""

Idle: TypeAlias = "subprocess.Popen[bytes]"


# --- the fold -----------------------------------------------------------


def intent(stem: str, *, host: str = HERE) -> Write:
    """An `intent`, through the real writer unless the host has to be faked."""

    def write(record: Path, workers_dir: Path) -> None:
        if host == HERE:
            record_intent(
                record,
                worker=stem,
                identifier=f"id-{stem}",
                key=stem,
                attempt=1,
                selection=selection_path(workers_dir, stem),
                argv=["python", "-c", "pass"],
                cwd=str(workers_dir),
                log_dir="logs",
            )
        else:
            line(
                record,
                {
                    "ts": utc_now(),
                    "type": INTENT,
                    "host": host,
                    "worker": stem,
                    "identifier": f"id-{stem}",
                    "selection": str(selection_path(workers_dir, stem)),
                },
            )

    return write


def pid_of(stem: str) -> int:
    """The pid the stub scan gives a worker.

    Derived rather than passed, so that a record and a scan can agree on one
    without every case in the table threading a number through — and so that a
    case where they *disagree* has to say so.
    """
    return 1000 + ord(stem[0])


def launched(stem: str, pid: int | None = None) -> Write:
    def write(record: Path, workers_dir: Path) -> None:
        record_launched(record, worker=stem, pid=pid_of(stem) if pid is None else pid)

    return write


def exited(stem: str) -> Write:
    def write(record: Path, workers_dir: Path) -> None:
        record_exited(record, worker=stem)

    return write


def raw(text: str) -> Write:
    """A line as it is, for the shapes a writer cannot produce."""

    def write(record: Path, workers_dir: Path) -> None:
        with record.open("a", encoding="utf-8") as f:
            f.write(text)

    return write


def line(record: Path, document: dict[str, Any]) -> None:
    with record.open("a", encoding="utf-8") as f:
        f.write(json.dumps(document) + "\n")


def selection_path(workers_dir: Path, stem: str) -> Path:
    return workers_dir / f"{stem}.json"


def write_selection(workers_dir: Path, stem: str) -> Path:
    """A real selection document for a worker.

    Real because a scanned worker with no record is identified by reading one —
    the lost-record path is not a stub away from the live one.
    """
    path = selection_path(workers_dir, stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        worker_selection(
            action(f"id-{stem}", key=stem), eval_set_id=EVAL_SET_ID, log_dir="logs"
        ).model_dump_json(exclude_none=True),
        encoding="utf-8",
    )
    return path


def alive_scan(workers_dir: Path, stems: list[str]) -> WorkerScan:
    """A scan reporting exactly these workers."""
    for stem in stems:
        write_selection(workers_dir, stem)
    found = [
        ScannedWorker(pid=pid_of(stem), selection=selection_path(workers_dir, stem))
        for stem in stems
    ]
    return lambda _: found


@dataclass(frozen=True)
class Case:
    """One record, one set of live processes, and what should come out."""

    id: str
    written: list[Write]
    alive: list[str] = field(default_factory=list[str])
    running: list[str] = field(default_factory=list[str])
    departed: list[str] = field(default_factory=list[str])
    pids: dict[str, int | None] = field(default_factory=dict[str, "int | None"])
    """Expected pid per departed worker, where the point of the case is the pid."""


CASES = [
    Case(
        id="an_intent_whose_spawn_never_returned_is_departed_with_no_pid",
        written=[intent("a")],
        departed=["a"],
        pids={"a": None},
    ),
    Case(
        id="an_intent_alone_is_enough_to_see_a_worker_running",
        # the window this record exists for: `launched` has not been written
        # yet, and the process is already there
        written=[intent("a")],
        alive=["a"],
        running=["a"],
    ),
    Case(
        id="a_launched_worker_still_in_the_table_is_running",
        written=[intent("a"), launched("a")],
        alive=["a"],
        running=["a"],
    ),
    Case(
        id="a_launched_worker_gone_from_the_table_is_departed_with_its_pid",
        written=[intent("a"), launched("a", 4242)],
        departed=["a"],
        pids={"a": 4242},
    ),
    Case(
        id="a_process_that_is_not_the_recorded_one_keeps_nothing_alive",
        # a leftover child of a dead worker, which inherited its selection path
        # and would otherwise hold the task open forever
        written=[intent("a"), launched("a", 9999)],
        alive=["a"],
        departed=["a"],
        pids={"a": 9999},
    ),
    Case(
        id="a_reaped_worker_is_neither",
        # including when something is still holding its selection open: the
        # record is final for a stem, and the next attempt gets its own
        written=[intent("a"), launched("a"), exited("a")],
        alive=["a"],
    ),
    Case(
        id="a_type_this_version_has_never_heard_of_is_not_damage",
        written=[
            intent("a"),
            raw(json.dumps({"ts": utc_now(), "type": "parked", "worker": "a"}) + "\n"),
            launched("a"),
        ],
        alive=["a"],
        running=["a"],
    ),
    Case(
        id="a_torn_last_line_costs_one_line",
        written=[intent("a"), launched("a", 77), raw('{"ts":"2026-08-23T00:00:00Z"')],
        departed=["a"],
        pids={"a": 77},
    ),
    Case(
        id="a_launched_with_no_intent_before_it_attaches_to_nothing",
        written=[launched("b")],
    ),
    Case(
        id="a_record_from_another_host_is_not_this_hosts_to_reap",
        written=[intent("a", host=ELSEWHERE), launched("a")],
    ),
    Case(
        id="a_worker_the_record_lost_is_still_running",
        written=[],
        alive=["a"],
        running=["a"],
    ),
    Case(
        id="one_of_each",
        written=[intent("a"), launched("a"), intent("b"), launched("b", 55)],
        alive=["a", "c"],
        running=["a", "c"],
        departed=["b"],
        pids={"b": 55},
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_record_and_the_table_resolve_together(case: Case, tmp_path: Path) -> None:
    workers_dir = tmp_path / ".steward" / "workers"
    workers_dir.mkdir(parents=True)
    record = tmp_path / ".steward" / "inflight.jsonl"
    scan = alive_scan(workers_dir, case.alive)
    for write in case.written:
        write(record, workers_dir)

    resolved = resolve_inflight(record, workers_dir, host=HERE, scan=scan)

    assert sorted(worker.worker for worker in resolved.running) == sorted(case.running)
    assert sorted(worker.worker for worker in resolved.departed) == sorted(
        case.departed
    )
    # every worker carries the identifier of the task it is running, whichever
    # of the two sources supplied it
    assert {worker.identifier for worker in resolved.running} == {
        f"id-{stem}" for stem in case.running
    }
    assert {
        worker.worker: worker.pid
        for worker in resolved.departed
        if worker.worker in case.pids
    } == case.pids


def test_a_spawn_that_never_starts_leaves_an_intent_and_nothing_else(
    tmp_path: Path,
) -> None:
    # a real failure rather than a patched one: a working directory that does
    # not exist is refused by the fork, after the record has been written
    workers = fleet(FIXTURES / "simple_evalset.py", tmp_path, cwd=tmp_path / "nowhere")

    with pytest.raises(OSError):
        workers.spawn(action("id-never-started"))

    types = [event.type for event in read_events(workers.inflight).events]
    assert types == [INTENT]
    # and the next resolve says so, rather than holding the task forever
    resolved = resolve_inflight(
        workers.inflight, workers.workers_dir, scan=lambda _: []
    )
    assert [worker.identifier for worker in resolved.departed] == ["id-never-started"]
    assert resolved.departed[0].pid is None


# --- live workers -------------------------------------------------------


def until(what: str, predicate: Callable[[], bool], timeout: float = 120) -> None:
    """Poll until something is true, or say what never became true."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def gated_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Fleet, Path]:
    """A fleet over the gated definition, and the file that releases it.

    Capture runs the definition too, so the gate is deliberately conditioned on
    worker mode: reading the manifest here costs nothing.
    """
    gate = tmp_path / "gate"
    monkeypatch.setenv("STEWARD_TEST_GATE", str(gate))
    return fleet(FIXTURES / "gated_evalset.py", tmp_path), gate


def kill(worker: SpawnedWorker) -> None:
    """Kill a worker and reap it, so its pid is not still claimed."""
    if worker.process.poll() is None:
        os.kill(worker.pid, signal.SIGKILL)
    worker.process.wait(timeout=60)


def test_a_worker_before_its_eval_is_visible_only_to_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window the whole record exists for.

    A worker that has started and not yet reached `eval_set()` has written no
    log and bound no control socket. Both ground-truth sources say it is not
    there; spawning on that answer runs the task twice.
    """
    workers, gate = gated_fleet(tmp_path, monkeypatch)
    manifest = read_eval_set(workers.definition, cwd=tmp_path)
    task = manifest.tasks[0]
    worker = workers.spawn(action(task.identifier, key=task.key))

    try:
        until(
            "the scan to see a worker it has no other evidence of",
            lambda: bool(scan_processes(workers.workers_dir)),
        )
        resolved = resolve_inflight(workers.inflight, workers.workers_dir)

        assert [item.identifier for item in resolved.running] == [task.identifier]
        assert resolved.running[0].pid == worker.pid
        assert resolved.departed == []
        # the window, asserted at both sources that cannot see it
        assert resolved.running[0].socket is None
        assert not list_eval_logs(str(tmp_path / "logs"))

        # so the task is not spawned a second time
        observed = observe_tasks(manifest, observe_logs(str(tmp_path / "logs")))
        plan = reconcile(manifest, resolved, observed, pool=POOL)
        assert plan.actions == []
        assert plan.summary.running == 1

        # and losing the record does not change the answer, because a worker's
        # selection document names the task it is running
        lost = resolve_inflight(tmp_path / "gone.jsonl", workers.workers_dir)
        assert [item.identifier for item in lost.running] == [task.identifier]

        # the window closes observably: once the eval starts, the worker binds
        # a control socket, which is what everything after this step talks to
        gate.touch()

        def bound() -> bool:
            live = resolve_inflight(workers.inflight, workers.workers_dir).running
            assert live, f"the worker exited before its eval:\n{output(worker)}"
            return live[0].socket is not None

        until("the worker to bind a control socket", bound)
    finally:
        kill(worker)


def test_a_worker_that_finished_is_reaped_and_not_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers, gate = gated_fleet(tmp_path, monkeypatch)
    gate.touch()
    manifest = read_eval_set(workers.definition, cwd=tmp_path)
    task = manifest.tasks[0]
    worker = workers.spawn(action(task.identifier, key=task.key))
    assert worker.process.wait(timeout=300) == 0, output(worker)

    resolved = resolve_inflight(workers.inflight, workers.workers_dir)

    assert resolved.running == []
    assert [item.worker for item in resolved.departed] == [worker.worker]
    assert resolved.departed[0].pid == worker.pid

    observed = observe_tasks(manifest, observe_logs(str(tmp_path / "logs")))
    plan = reconcile(manifest, resolved, observed, pool=POOL)

    # reaped, and not run again: the log landed, so there is nothing left to do
    assert plan.actions == [ReapWorker(resolved.departed[0])]
    assert not [item for item in plan.actions if isinstance(item, SpawnWorker)]


def test_a_worker_killed_before_its_log_lands_is_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers, _gate = gated_fleet(tmp_path, monkeypatch)
    manifest = read_eval_set(workers.definition, cwd=tmp_path)
    task = manifest.tasks[0]
    # the gate is never opened: the worker dies in the window before its eval,
    # which is where a task can be lost without leaving a log behind
    worker = workers.spawn(action(task.identifier, key=task.key))
    until(
        "the worker to appear in the process table",
        lambda: bool(scan_processes(workers.workers_dir)),
    )
    kill(worker)

    resolved = resolve_inflight(workers.inflight, workers.workers_dir)

    assert resolved.running == []
    assert [item.pid for item in resolved.departed] == [worker.pid]

    observed = observe_tasks(manifest, observe_logs(str(tmp_path / "logs")))
    plan = reconcile(manifest, resolved, observed, pool=POOL)
    spawns = [item for item in plan.actions if isinstance(item, SpawnWorker)]

    assert plan.actions[0] == ReapWorker(resolved.departed[0])
    # nothing landed, so the second attempt starts fresh rather than resuming
    assert [item.identifier for item in spawns] == [task.identifier]
    assert spawns[0].resume is None
    assert spawns[0].attempt == 1


def test_the_scan_answers_for_this_workspace_only(tmp_path: Path) -> None:
    # a workspace is a directory, so several Stewards on one machine is an
    # ordinary shape rather than an exotic one, and every worker of every one
    # of them carries the same marker. Bounding the scan by the workers
    # directory is what keeps them out of each other's answers -- and bounding
    # it there rather than at the workspace root is what makes the nested case
    # fall out for free, in both directions
    mine = tmp_path / "mine" / ".steward" / "workers"
    theirs = tmp_path / "theirs" / ".steward" / "workers"
    nested = tmp_path / "mine" / "sub" / ".steward" / "workers"
    ours, other, inner = (idle(dir / "w.json") for dir in (mine, theirs, nested))

    try:
        found = scan_processes(mine)
        from_the_nested_one = scan_processes(nested)
    finally:
        for process in (ours, other, inner):
            process.kill()
            process.wait(timeout=60)

    assert [(item.pid, item.selection) for item in found] == [
        (ours.pid, (mine / "w.json").resolve())
    ]
    assert [item.pid for item in from_the_nested_one] == [inner.pid]


def idle(selection: Path, script: str = "import time; time.sleep(120)") -> Idle:
    """A process that does nothing but claim a selection document.

    Real rather than simulated, because what is being tested is that the
    environment of a live process can be read at all — the measurement the
    scan's affordability rests on.
    """
    selection.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "-c", script],
        env={**os.environ, INSPECT_EVAL_SET_SELECTION: str(selection)},
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


SPAWNS_A_CHILD = (
    "import subprocess, sys, time; "
    "print(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])"
    ".pid, flush=True); "
    "time.sleep(120)"
)
"""What an eval does constantly — a sandbox's `docker`, a frontend's `uv` — reduced to the one property that matters here: the child inherits the marker."""


def idle_tree(selection: Path) -> tuple[Idle, int]:
    """A process claiming a selection document, and the child that inherited it."""
    process = idle(selection, SPAWNS_A_CHILD)
    assert process.stdout is not None
    return process, int(process.stdout.readline())


def test_a_workers_children_are_not_mistaken_for_the_worker(tmp_path: Path) -> None:
    # every subprocess an eval starts inherits INSPECT_EVAL_SET_SELECTION, so
    # the marker alone matches a whole subtree. Taking the wrong member of it
    # loses the worker's pid -- which is its control socket, and what a signal
    # would be sent to -- and can hold a task open on a leftover child forever
    workers_dir = tmp_path / ".steward" / "workers"
    record = tmp_path / ".steward" / "inflight.jsonl"
    selection = write_selection(workers_dir, "a")
    worker, child = idle_tree(selection)

    try:
        intent("a")(record, workers_dir)
        record_launched(record, worker="a", pid=worker.pid)

        # the subtree contributes its root, and only its root
        assert [item.pid for item in scan_processes(workers_dir)] == [worker.pid]
        assert [item.pid for item in resolve_inflight(record, workers_dir).running] == [
            worker.pid
        ]

        worker.kill()
        worker.wait(timeout=60)
        until(
            "the child to be all that is left running the selection",
            lambda: [item.pid for item in scan_processes(workers_dir)] == [child],
        )
        resolved = resolve_inflight(record, workers_dir)

        # the recorded pid is gone, so the worker is gone -- whatever is still
        # holding its selection open
        assert resolved.running == []
        assert [item.pid for item in resolved.departed] == [worker.pid]

        # and this is what losing the record costs: with no pid to check the
        # orphan against, it reads as the worker. A degradation, not a hole --
        # the alternative is trusting a pid nothing corroborates
        lost = resolve_inflight(tmp_path / "gone.jsonl", workers_dir)
        assert [item.pid for item in lost.running] == [child]
    finally:
        with suppress(ProcessLookupError):
            # orphaned by its parent's death, so it is init's to reap, not ours
            os.kill(child, signal.SIGKILL)
