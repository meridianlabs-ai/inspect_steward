"""Breaking a run on purpose, at the points where the recovery claims live.

Most of testing.md §4's faults are already falsified somewhere — each arrived
as the natural test of the step that built its subject, which is the strategy
working rather than an accident. What is here is the remainder: the two that
had no test at all, and the one that had only its benign half.

All three turn on the same question. Steward asks *what is running* of the
process table, and until this step it asked *what is it running* of a file. The
faults below are the ones that delete the file.
"""

import shutil
from pathlib import Path

import pytest
from inspect_ai.log import list_eval_logs
from inspect_steward import Manifest, read_eval_set
from inspect_steward._evalset.observe import observe_logs, observe_tasks
from inspect_steward._schedule import (
    Action,
    Pool,
    ReapWorker,
    SpawnWorker,
    reconcile,
)
from inspect_steward._worker import Fleet, SpawnedWorker, resolve_inflight

from .._fault import FAULT_FIXTURE, Fault, arm, kill
from ._fleet import FIXTURES, action, fleet, output


def start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> tuple[Fleet, Manifest, SpawnedWorker, Fault]:
    """Capture the faulty definition, spawn its one task, and wait for the fault."""
    injected = arm(monkeypatch, tmp_path, fault)
    workers = fleet(FIXTURES / FAULT_FIXTURE, tmp_path)
    manifest = read_eval_set(workers.definition, cwd=tmp_path)
    task = manifest.tasks[0]
    worker = workers.spawn(action(task.identifier, key=task.key))
    injected.reached()
    return workers, manifest, worker, injected


def next_actions(workers: Fleet, manifest: Manifest) -> list[Action]:
    """What a tend would do now.

    The manifest is re-captured by the caller rather than read from `.steward/`, which is what a tend after this fault would have to do — the manifest lives there too.
    """
    inflight = resolve_inflight(workers.inflight, workers.workers_dir)
    observed = observe_tasks(manifest, observe_logs(workers.log_dir))
    return list(reconcile(manifest, inflight, observed, pool=Pool()).actions)


def spawns(actions: list[Action]) -> list[SpawnWorker]:
    return [item for item in actions if isinstance(item, SpawnWorker)]


def test_a_worker_survives_its_state_directory_being_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fault this step was written to find.

    `.steward/` holds the in-flight record *and* every selection document, so
    deleting it mid-eval takes both halves of what used to identify a running
    worker. A worker that cannot be identified is a task with a `started` log
    and nothing running it — which reconciles to a respawn, resuming the log
    the first worker is still writing.
    """
    workers, manifest, worker, injected = start(tmp_path, monkeypatch, "run:hang")

    try:
        shutil.rmtree(tmp_path / ".steward")

        # the worker is still there, and still says what it is running
        inflight = resolve_inflight(workers.inflight, workers.workers_dir)
        assert [item.identifiers for item in inflight.running] == [
            (manifest.tasks[0].identifier,)
        ]
        assert inflight.running[0].pid == worker.pid
        assert spawns(next_actions(workers, manifest)) == []
    finally:
        injected.release()

    assert worker.process.wait(timeout=300) == 0, output(worker)
    # one worker ran, so one log landed -- no duplicate resumed over it
    assert len(list_eval_logs(str(workers.log_dir))) == 1
    assert spawns(next_actions(workers, manifest)) == []


def test_a_starting_worker_loses_the_document_it_has_not_read_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same deletion one moment earlier, which is a different story.

    Before the boundary a worker has not read its selection document, so
    deleting it is fatal — and that is the right outcome rather than a hazard:
    a dead worker should be respawned, and the identity in its environment is
    what keeps the tend before it from respawning it while it is still alive.
    """
    workers, manifest, worker, injected = start(tmp_path, monkeypatch, "pre:hang")
    identifier = manifest.tasks[0].identifier

    shutil.rmtree(tmp_path / ".steward")

    # held before the boundary and visible, so nothing is scheduled over it
    assert spawns(next_actions(workers, manifest)) == []

    injected.release()
    assert worker.process.wait(timeout=300) != 0, "the worker should not have run"
    assert not list_eval_logs(str(workers.log_dir))

    # and now that it is gone, exactly one respawn -- from nothing, since the
    # dead worker left no log to resume
    scheduled = spawns(next_actions(workers, manifest))
    assert [item.identifiers for item in scheduled] == [(identifier,)]
    assert scheduled[0].first.resume is None


def test_a_worker_killed_after_its_log_lands_is_not_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaping is correct: finished work is not respawned because its worker died.

    The state is only observable by holding the process open after `eval_set()`
    has returned, which is what `post` exists for.
    """
    workers, manifest, worker, _held = start(tmp_path, monkeypatch, "post:hang")

    # `post` is reached only after `eval_set()` returns, so the log is already
    # on disk and the process is still alive to be killed
    assert len(list_eval_logs(str(workers.log_dir))) == 1
    kill(worker)

    actions = next_actions(workers, manifest)
    assert spawns(actions) == []
    # gone, and reported gone, which is what lets its slot be reused
    assert [
        item.worker.identifiers for item in actions if isinstance(item, ReapWorker)
    ] == [(manifest.tasks[0].identifier,)]


def test_the_fault_is_not_armed_during_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # capture executes the definition too, so arming on the variable alone
    # would make every read of this fixture crash. Arming is conditioned on
    # worker mode instead, which is what keeps reading a manifest free
    arm(monkeypatch, tmp_path, "pre:crash")
    manifest = read_eval_set(FIXTURES / FAULT_FIXTURE, cwd=tmp_path)
    assert len(manifest.tasks) == 1


def test_a_worker_that_crashes_at_a_point_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the other half of the fixture's contract: `hang` is what the tests above
    # use, and `crash` is what makes a broken worker cheap for later steps
    arm(monkeypatch, tmp_path, "pre:crash")
    workers = fleet(FIXTURES / FAULT_FIXTURE, tmp_path)
    manifest = read_eval_set(workers.definition, cwd=tmp_path)
    worker = workers.spawn(action(manifest.tasks[0].identifier))

    assert worker.process.wait(timeout=300) != 0
    assert "fault injected at pre" in output(worker)
