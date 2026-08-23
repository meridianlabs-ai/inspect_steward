"""The decision function, as a table.

No processes, no clock, no filesystem except where a log's *content* is the
subject — an all-`missing` observation is `observe_tasks(manifest,
ObservedLogs(log_dir=...))` and touches nothing at all. That is what keeping
`reconcile` pure was worth insisting on (execution.md, *The reconcile core*).
"""

from pathlib import Path
from typing import Any

import pytest
from inspect_steward._evalset.manifest import Manifest
from inspect_steward._evalset.observe import (
    IncompleteReason,
    ObservedLogs,
    ObservedTasks,
    TaskState,
    observe_logs,
    observe_tasks,
)
from inspect_steward._schedule import (
    DEFAULT_MAX_SAMPLES,
    DEFAULT_MAX_WORKERS,
    InFlight,
    ManifestVersionError,
    Pool,
    ReapWorker,
    Reconciliation,
    RunningWorker,
    SpawnWorker,
    reconcile,
)

from .._logs import SynthTask, synth_manifest, write_log

TASK = SynthTask("probe", samples=10, epochs=1)

POOL = Pool(max_workers=8)


def nothing_run(manifest: Manifest) -> ObservedTasks:
    """The observation of an empty log directory — no filesystem involved."""
    return observe_tasks(manifest, ObservedLogs(log_dir="logs"))


def spawns(result: Reconciliation) -> list[SpawnWorker]:
    return [action for action in result.actions if isinstance(action, SpawnWorker)]


def keys(workers: list[SpawnWorker]) -> list[str]:
    return [worker.key for worker in workers]


@pytest.mark.parametrize(
    ("log", "spawned", "reason"),
    [
        pytest.param(None, True, None, id="missing_spawns_fresh"),
        pytest.param({}, False, None, id="complete_spawns_nothing"),
        pytest.param({"completed": 7}, False, None, id="errored_samples_are_not_work"),
        pytest.param({"total": 6}, True, IncompleteReason.SHORT, id="short_resumes"),
        pytest.param(
            {"invalidated": True},
            True,
            IncompleteReason.INVALIDATED,
            id="invalidated_resumes",
        ),
        pytest.param(
            {"error": "boom"}, True, IncompleteReason.ERROR, id="errored_resumes"
        ),
        pytest.param(
            {"status": "cancelled", "total": 4},
            True,
            IncompleteReason.CANCELLED,
            id="cancelled_resumes",
        ),
    ],
)
def test_one_task(
    log: dict[str, Any] | None,
    spawned: bool,
    reason: IncompleteReason | None,
    tmp_path: Path,
) -> None:
    manifest = synth_manifest([TASK])
    if log is not None:
        write_log(tmp_path, TASK, **log)

    result = reconcile(
        manifest, InFlight(), observe_tasks(manifest, observe_logs(tmp_path)), pool=POOL
    )

    assert len(spawns(result)) == (1 if spawned else 0)
    if spawned:
        worker = spawns(result)[0]
        assert worker.identifier == TASK.identifier
        assert worker.reason == reason
        # a task that has run before resumes it; one that has not starts fresh
        assert (worker.resume is not None) == (log is not None)


def test_a_crashed_worker_is_indistinguishable_in_the_log_directory(
    tmp_path: Path,
) -> None:
    """The single most important thing this function does.

    A worker running now and a worker that died mid-run leave exactly the same
    thing behind — a `started` log with no results. Only the in-flight record
    separates them, and getting it wrong means either double-spawning a live
    task or never recovering a dead one.
    """
    manifest = synth_manifest([TASK])
    write_log(tmp_path, TASK, status="started")
    observed = observe_tasks(manifest, observe_logs(tmp_path))
    live = RunningWorker(identifier=TASK.identifier, pid=4242, host="here")

    crashed = reconcile(manifest, InFlight(), observed, pool=POOL)
    alive = reconcile(manifest, InFlight(running=[live]), observed, pool=POOL)

    assert len(spawns(crashed)) == 1
    assert spawns(crashed)[0].reason == IncompleteReason.STARTED
    assert spawns(crashed)[0].resume is not None
    assert spawns(alive) == []
    assert alive.summary.running == 1


def test_an_orphan_is_reported_and_not_spawned(tmp_path: Path) -> None:
    removed = SynthTask("removed")
    manifest = synth_manifest([TASK])
    write_log(tmp_path, removed)

    result = reconcile(
        manifest, InFlight(), observe_tasks(manifest, observe_logs(tmp_path)), pool=POOL
    )

    assert keys(spawns(result)) == [manifest.tasks[0].key]
    assert result.summary.orphans == [removed.identifier]
    assert result.summary.states[TaskState.ORPHANED.value] == 1
    # the manifest's own task count excludes them
    assert result.summary.tasks == 1


def test_spawn_order_transposes_the_crossing() -> None:
    # as eval_resolve_tasks enumerates: models on the outside, tasks within
    # (verified by tests/evalset/test_read.py::test_read_eval_set_sweep)
    model_major = [
        SynthTask("sweep", args={"difficulty": d}, model=m)
        for m in ("mockllm/model", "mockllm/model2")
        for d in ("easy", "hard")
    ]
    manifest = synth_manifest(model_major)

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool(16))

    # both authored orders survive; only the nesting flips, so each task
    # completes on every model before the next task starts
    assert keys(spawns(result)) == [
        "sweep[default]@mockllm/model (difficulty=easy)",
        "sweep[default]@mockllm/model2 (difficulty=easy)",
        "sweep[default]@mockllm/model (difficulty=hard)",
        "sweep[default]@mockllm/model2 (difficulty=hard)",
    ]


def test_the_transposition_is_a_no_op_for_a_single_model_sweep() -> None:
    tasks = [SynthTask("sweep", args={"n": n}) for n in range(4)]
    manifest = synth_manifest(tasks)

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool(16))

    assert [worker.identifier for worker in spawns(result)] == [
        task.identifier for task in tasks
    ]


def test_one_task_per_model_goes_out_first() -> None:
    # what eval_set() buys with a second knob (max_tasks' per-model floor),
    # spawn order gives for free whenever the ceiling covers the model count
    models = ("mockllm/model", "mockllm/model2", "mockllm/model3")
    manifest = synth_manifest(
        [SynthTask("t", args={"n": n}, model=m) for m in models for n in range(4)]
    )

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool(3))

    model_of = {task.identifier: task.model for task in manifest.tasks}
    assert {model_of[worker.identifier] for worker in spawns(result)} == set(models)


def test_the_ceiling_splits_pending_into_actions_and_a_queue() -> None:
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(10)])

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool(4))

    assert len(spawns(result)) == 4
    assert len(result.queued) == 6
    # the queue is the same decision deferred, in the same order
    assert result.queued[0].key.endswith("(n=4)")
    assert result.summary.spawning == 4 and result.summary.queued == 6


def test_below_the_ceiling_there_is_no_queue() -> None:
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(3)])

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool(8))

    assert len(spawns(result)) == 3
    assert result.queued == []


def test_running_workers_take_slots_from_the_ceiling() -> None:
    # the ceiling is on total workers, not on this turn's spawns: eight running
    # and five pending under a ceiling of ten spawns two, not five
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(13)])
    eight = [
        RunningWorker(identifier=task.identifier, pid=1000 + n, host="here")
        for n, task in enumerate(manifest.tasks[:8])
    ]

    result = reconcile(
        manifest, InFlight(running=eight), nothing_run(manifest), pool=Pool()
    )

    assert len(spawns(result)) == 2
    assert len(result.queued) == 3


def test_the_default_ceiling_owes_nothing_to_the_hardware() -> None:
    # a worker is on the CPU in bursts and waiting on a model in between, so
    # the ceiling is a resource guard the user tunes, not a core count
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(30)])

    default = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool())
    cranked = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool(30))

    assert DEFAULT_MAX_WORKERS == 10
    assert len(spawns(default)) == 10 and len(default.queued) == 20
    assert len(spawns(cranked)) == 30 and cranked.queued == []


def test_convergence_and_idempotence() -> None:
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(3)])
    observed = nothing_run(manifest)

    first = reconcile(manifest, InFlight(), observed, pool=POOL)
    again = reconcile(manifest, InFlight(), observed, pool=POOL)
    # a repeated call with unchanged inputs decides the same thing
    assert first == again

    # once the spawns have happened, the next call has nothing to do
    launched = InFlight(
        running=[
            RunningWorker(identifier=worker.identifier, pid=n, host="here")
            for n, worker in enumerate(spawns(first))
        ]
    )
    settled = reconcile(manifest, launched, observed, pool=POOL)

    assert settled.actions == []
    assert settled.queued == []
    assert settled.summary.running == 3


def test_a_departed_worker_is_reaped_and_holds_no_slot() -> None:
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(2)])
    gone = RunningWorker(identifier=manifest.tasks[0].identifier, pid=99, host="here")

    result = reconcile(
        manifest, InFlight(departed=[gone]), nothing_run(manifest), pool=Pool(2)
    )

    assert result.actions[0] == ReapWorker(gone)
    # both tasks still spawn: a dead worker occupies nothing, and the one it
    # was running is pending again
    assert len(spawns(result)) == 2
    assert result.summary.running == 0


def test_paused_schedules_nothing_and_says_so() -> None:
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(3)])

    result = reconcile(
        manifest, InFlight(), nothing_run(manifest), pool=POOL, paused=True
    )

    assert spawns(result) == []
    # not "nothing to do" — three tasks waiting for the run to resume
    assert len(result.queued) == 3
    assert result.summary.paused
    assert result.summary.states[TaskState.MISSING.value] == 3


def test_paused_still_reaps() -> None:
    manifest = synth_manifest([TASK])
    gone = RunningWorker(identifier=TASK.identifier, pid=7, host="here")

    result = reconcile(
        manifest,
        InFlight(departed=[gone]),
        nothing_run(manifest),
        pool=POOL,
        paused=True,
    )

    assert result.actions == [ReapWorker(gone)]


@pytest.mark.parametrize(
    ("options", "pool", "expected"),
    [
        pytest.param({}, POOL, DEFAULT_MAX_SAMPLES, id="default"),
        pytest.param({}, Pool(8, max_samples=12), 12, id="pool_override"),
        # not recorded by capture today; read anyway so the day it lands the
        # definition's value simply starts winning
        pytest.param({"max_samples": 60}, POOL, 60, id="the_definition_wins"),
        pytest.param({"max_samples": None}, POOL, DEFAULT_MAX_SAMPLES, id="unset"),
    ],
)
def test_max_samples(options: dict[str, Any], pool: Pool, expected: int) -> None:
    manifest = synth_manifest([TASK], **options)

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=pool)

    assert spawns(result)[0].max_samples == expected


def test_attempt_counts_the_logs_already_there(tmp_path: Path) -> None:
    manifest = synth_manifest([TASK])
    write_log(
        tmp_path, TASK, status="error", error="one", created="2026-08-23T18:00:00+00:00"
    )
    write_log(
        tmp_path, TASK, status="error", error="two", created="2026-08-23T20:00:00+00:00"
    )

    result = reconcile(
        manifest, InFlight(), observe_tasks(manifest, observe_logs(tmp_path)), pool=POOL
    )

    assert spawns(result)[0].attempt == 3


def test_a_manifest_from_a_different_inspect_is_refused() -> None:
    # unmatchable identifiers make every task read missing and every log read
    # orphaned, so a finished sweep would re-run from scratch. The refusal is
    # an exception precisely because a summary carrying it would look normal.
    manifest = synth_manifest([TASK])
    stale = manifest.model_copy(
        update={"identifier_version": manifest.identifier_version + 1}
    )

    with pytest.raises(ManifestVersionError, match="steward launch"):
        reconcile(stale, InFlight(), nothing_run(manifest), pool=POOL)


def test_the_summary_counts_what_a_status_line_needs(tmp_path: Path) -> None:
    done, short, running, never = (
        SynthTask("done"),
        SynthTask("short"),
        SynthTask("running"),
        SynthTask("never"),
    )
    manifest = synth_manifest([done, short, running, never])
    write_log(tmp_path, done)
    write_log(tmp_path, short, total=4)
    write_log(tmp_path, running, status="started")
    write_log(tmp_path, SynthTask("removed"))
    logs = observe_logs(tmp_path)

    result = reconcile(
        manifest,
        InFlight(running=[RunningWorker(running.identifier, pid=1, host="here")]),
        observe_tasks(manifest, logs),
        pool=POOL,
    )

    assert result.summary.states == {
        "complete": 1,
        "incomplete": 2,
        "missing": 1,
        "orphaned": 1,
    }
    assert result.summary.reasons["short"] == 1
    assert result.summary.reasons["started"] == 1
    assert result.summary.running == 1
    assert result.summary.spawning == 2
    assert result.summary.unreadable == 0
    assert result.summary.max_workers == 8
