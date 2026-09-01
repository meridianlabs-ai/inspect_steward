"""The decision function, as a table.

No processes, no clock, no filesystem except where a log's *content* is the
subject — an all-`missing` observation is `observe_tasks(manifest,
ObservedLogs(log_dir=...))` and touches nothing at all. That is what keeping
`reconcile` pure was worth insisting on (execution.md, *The reconcile core*).
"""

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
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
    DEFAULT_SAMPLES_RAMP,
    DEFAULT_STALL_AFTER,
    ArchiveLog,
    DepartedWorker,
    InFlight,
    ManifestVersionError,
    Pool,
    ReapWorker,
    Reconciliation,
    RunningWorker,
    SpawnTask,
    SpawnWorker,
    reconcile,
    resolve_max_tasks,
    resolve_samples_ramp,
)

from .._logs import SynthTask, synth_manifest, write_log

TASK = SynthTask("probe", samples=10, epochs=1)

POOL = Pool(max_workers=8)


def nothing_run(manifest: Manifest) -> ObservedTasks:
    """The observation of an empty log directory — no filesystem involved."""
    return observe_tasks(manifest, ObservedLogs(log_dir="logs"))


def live(identifier: str, *, pid: int = 4242) -> RunningWorker:
    """A worker confirmed alive.

    The stem and the socket are synthesized rather than passed: `reconcile`
    reads neither, and a test that supplied them would be asserting they are
    carried rather than that they matter.
    """
    return RunningWorker(
        worker=f"w{pid}", identifiers=(identifier,), pid=pid, host="here"
    )


def dead(identifier: str, *, pid: int | None = 99) -> DepartedWorker:
    """A worker the record accounts for that is no longer running."""
    return DepartedWorker(
        worker=f"w{pid}", identifiers=(identifier,), pid=pid, host="here"
    )


def spawns(result: Reconciliation) -> list[SpawnWorker]:
    return [action for action in result.actions if isinstance(action, SpawnWorker)]


def archives(result: Reconciliation) -> list[ArchiveLog]:
    return [action for action in result.actions if isinstance(action, ArchiveLog)]


def planned(result: Reconciliation) -> list[SpawnTask]:
    """Every task this turn would start, flattened out of the processes hosting them.

    What most of these tests are actually about: which tasks run, in what order,
    resuming what. How they are divided into processes is the pour's business
    and is tested against `pour` directly.
    """
    return [task for worker in spawns(result) for task in worker.tasks]


def keys(tasks: list[SpawnTask]) -> list[str]:
    return [task.key for task in tasks]


def attempts(log_dir: Path, task: SynthTask, finished: list[int]) -> None:
    """One log per attempt, oldest first, each finishing `finished` samples.

    Short of the manifest's count every time, so every attempt reads incomplete
    and the only thing that varies between them is whether it got further.
    """
    for n, count in enumerate(finished):
        write_log(
            log_dir,
            task,
            total=count,
            completed=count,
            created=f"2026-08-23T{10 + n:02d}:00:00+00:00",
        )


def crashes(count: int, *, since: int = 20) -> list[str]:
    """Start times for `count` attempts that ended having landed no log.

    On the same day `attempts` writes into and by the hour, so a test can place
    them before or after the logs it wrote — which is the whole question the
    guard has to answer about them.
    """
    return [f"2026-08-23T{since + n:02d}:00:00+00:00" for n in range(count)]


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
        worker = planned(result)[0]
        assert worker.identifier == TASK.identifier
        assert worker.reason == reason
        # a task that has run before resumes it; one that has not starts fresh
        assert (worker.resume is not None) == (log is not None)


def test_a_redirected_task_is_spawned_without_its_prior_log(tmp_path: Path) -> None:
    """The one reason that must not resume, and the reason it must not.

    A changed sandbox or gateway leaves the sample *set* identical, so resume
    would look every sample up in the prior log, find every one of them, reuse
    every one, and finish having run nothing — the task reported as re-run and
    byte-identical to the stale one it replaced. Nothing about the lookup
    fails; what changed is that the answers are no longer worth having, which
    only the reason knows.
    """
    manifest = synth_manifest([TASK]).model_copy(
        update={"overrides": EvalSetOverrides(model_base_url="https://new.example/v1")}
    )
    write_log(tmp_path, TASK, model_base_url="https://old.example/v1")

    result = reconcile(
        manifest, InFlight(), observe_tasks(manifest, observe_logs(tmp_path)), pool=POOL
    )

    (worker,) = planned(result)
    assert worker.reason is IncompleteReason.REDIRECTED
    assert worker.resume is None

    # and the prior log is still there, as a superseded attempt
    assert list(tmp_path.glob("*.json"))


def test_a_reshaped_task_still_resumes(tmp_path: Path) -> None:
    # the slice moved, so the samples still wanted were answered under settings
    # still in force -- re-running the ones that overlap would be pure waste
    manifest = synth_manifest([TASK], limit=(5, 15))
    write_log(tmp_path, TASK, selection={"limit": 10})

    result = reconcile(
        manifest, InFlight(), observe_tasks(manifest, observe_logs(tmp_path)), pool=POOL
    )

    (worker,) = planned(result)
    assert worker.reason is IncompleteReason.RESHAPED
    assert worker.resume is not None


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
    crashed = reconcile(manifest, InFlight(), observed, pool=POOL)
    alive = reconcile(
        manifest, InFlight(running=[live(TASK.identifier)]), observed, pool=POOL
    )

    assert len(spawns(crashed)) == 1
    assert planned(crashed)[0].reason == IncompleteReason.STARTED
    assert planned(crashed)[0].resume is not None
    assert spawns(alive) == []
    assert alive.summary.running == 1


def test_an_orphan_is_reported_and_not_spawned(tmp_path: Path) -> None:
    removed = SynthTask("removed")
    manifest = synth_manifest([TASK])
    write_log(tmp_path, removed)

    result = reconcile(
        manifest, InFlight(), observe_tasks(manifest, observe_logs(tmp_path)), pool=POOL
    )

    assert keys(planned(result)) == [manifest.tasks[0].key]
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
    assert keys(planned(result)) == [
        "sweep[default]@mockllm/model (difficulty=easy)",
        "sweep[default]@mockllm/model2 (difficulty=easy)",
        "sweep[default]@mockllm/model (difficulty=hard)",
        "sweep[default]@mockllm/model2 (difficulty=hard)",
    ]


def test_the_transposition_is_a_no_op_for_a_single_model_sweep() -> None:
    tasks = [SynthTask("sweep", args={"n": n}) for n in range(4)]
    manifest = synth_manifest(tasks)

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool(16))

    assert [task.identifier for task in planned(result)] == [
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
    assert {model_of[task.identifier] for task in planned(result)} == set(models)


def test_max_tasks_splits_pending_into_actions_and_a_queue() -> None:
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(10)])

    result = reconcile(
        manifest, InFlight(), nothing_run(manifest), pool=Pool(max_tasks=4)
    )

    assert len(spawns(result)) == 4
    assert len(result.queued) == 6
    # the queue is the same decision deferred, in the same order
    assert result.queued[0].key.endswith("(n=4)")
    assert result.summary.spawning == 4 and result.summary.queued == 6


def test_fewer_processes_than_tasks_is_a_queue_only_when_max_tasks_says_so() -> None:
    # `max_workers` alone never queues anything: it says how many processes the
    # run uses, and everything pending is poured into them
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(3)])

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool(8))

    assert len(spawns(result)) == 3
    assert result.queued == []


def test_running_tasks_count_against_max_tasks() -> None:
    # the bound is on tasks in flight, not on this turn's spawns: eight running
    # and five pending under a limit of ten places two, not five
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(13)])
    eight = [
        live(task.identifier, pid=1000 + n) for n, task in enumerate(manifest.tasks[:8])
    ]

    result = reconcile(
        manifest,
        InFlight(running=eight),
        nothing_run(manifest),
        pool=Pool(max_tasks=10),
    )

    assert sum(len(spawn.tasks) for spawn in spawns(result)) == 2
    assert len(result.queued) == 3


def test_running_workers_count_against_max_workers() -> None:
    # and the process bound is counted the same way, one turn at a time: eight
    # processes alive under a limit of ten leaves room for two more, which the
    # five pending tasks are poured into rather than queueing behind
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(13)])
    eight = [
        live(task.identifier, pid=1000 + n) for n, task in enumerate(manifest.tasks[:8])
    ]

    result = reconcile(
        manifest,
        InFlight(running=eight),
        nothing_run(manifest),
        pool=Pool(max_workers=10),
    )

    assert [len(spawn.tasks) for spawn in spawns(result)] == [3, 2]
    assert result.queued == []


def test_an_unshaped_run_puts_every_task_in_a_process_of_its_own() -> None:
    # both knobs unbounded is the default, and it is the widest possible shape:
    # nothing waits and nothing shares a process
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(30)])

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=Pool())

    assert len(spawns(result)) == 30
    assert all(len(spawn.tasks) == 1 for spawn in spawns(result))
    assert result.queued == []


def test_convergence_and_idempotence() -> None:
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(3)])
    observed = nothing_run(manifest)

    first = reconcile(manifest, InFlight(), observed, pool=POOL)
    again = reconcile(manifest, InFlight(), observed, pool=POOL)
    # a repeated call with unchanged inputs decides the same thing
    assert first == again

    # once the spawns have happened, the next call has nothing to do
    launched = InFlight(
        running=[live(task.identifier, pid=n) for n, task in enumerate(planned(first))]
    )
    settled = reconcile(manifest, launched, observed, pool=POOL)

    assert settled.actions == []
    assert settled.queued == []
    assert settled.summary.running == 3


def test_a_departed_worker_is_reaped_and_holds_no_slot() -> None:
    manifest = synth_manifest([SynthTask("t", args={"n": n}) for n in range(2)])
    gone = dead(manifest.tasks[0].identifier)

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
    gone = dead(TASK.identifier, pid=7)

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
        pytest.param({}, POOL, DEFAULT_MAX_SAMPLES, id="nobody_asked"),
        # the definition's author knows the workload, so it beats Steward's
        # fallback — which is why Pool.max_samples defaults to None rather
        # than to DEFAULT_MAX_SAMPLES
        pytest.param({"max_samples": 60}, POOL, 60, id="the_definition_beats_default"),
        pytest.param({"max_samples": None}, POOL, DEFAULT_MAX_SAMPLES, id="unset"),
        pytest.param({}, Pool(max_samples=12), 12, id="the_operator_asked"),
        # and someone who typed a number for this run outranks the definition
        pytest.param(
            {"max_samples": 60},
            Pool(max_samples=12),
            12,
            id="the_operator_beats_the_definition",
        ),
        # a manifest from another version could carry anything under this key
        pytest.param(
            {"max_samples": "lots"}, POOL, DEFAULT_MAX_SAMPLES, id="nonsense_ignored"
        ),
    ],
)
def test_max_samples(options: dict[str, Any], pool: Pool, expected: int) -> None:
    manifest = synth_manifest([TASK], **options)

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=pool)

    assert spawns(result)[0].max_samples == expected


# --- the machine's sandbox budget, applied at spawn -----------------------


def test_a_spawn_is_clamped_to_its_share_of_the_sandbox_budget() -> None:
    # every worker computes the Docker provider's `2 x cores` for itself, so a
    # fleet that starts each task at the ramp's floor asks one host for N times
    # what it says it supports. The floor is the number that multiplies
    manifest = synth_manifest([SynthTask("sweep", args={"n": n}) for n in range(10)])

    result = reconcile(
        manifest, InFlight(), nothing_run(manifest), pool=Pool(16), budget=28
    )

    assert [worker.max_samples for worker in spawns(result)] == [2] * 10


def test_the_budget_share_may_land_below_the_ramps_floor() -> None:
    manifest = synth_manifest([TASK])

    unbounded = reconcile(manifest, InFlight(), nothing_run(manifest), pool=POOL)
    bounded = reconcile(
        manifest, InFlight(), nothing_run(manifest), pool=POOL, budget=6
    )

    assert unbounded.actions and spawns(unbounded)[0].max_samples > 6
    assert spawns(bounded)[0].max_samples == 6


def test_no_budget_leaves_the_resolved_level_alone() -> None:
    # an elastic provider caps nothing, and so does the first tend of a run
    # whose definition declared no `max_sandboxes`
    manifest = synth_manifest([TASK])

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=POOL)

    assert spawns(result)[0].max_samples == DEFAULT_MAX_SAMPLES


def test_the_budget_outranks_a_climbed_level_on_respawn() -> None:
    # the replay clamps a recorded level into the authorized range, and the
    # budget clamps what comes out of that -- otherwise a task cut to 3 by an
    # over-committed host comes back at the range's floor
    manifest = synth_manifest([TASK])

    result = reconcile(
        manifest,
        InFlight(),
        nothing_run(manifest),
        pool=POOL,
        levels={TASK.identifier: 200},
        budget=5,
    )

    assert spawns(result)[0].max_samples == 5


def test_a_pinned_setpoint_is_not_clamped_by_the_budget() -> None:
    # a pin is a number a person chose. A pinned fleet can still overshoot its
    # own `max_sandboxes`, and that is two numbers the same person owns --
    # reported rather than resolved (scheduling.md §3.6)
    manifest = synth_manifest([SynthTask("sweep", args={"n": n}) for n in range(10)])

    result = reconcile(
        manifest,
        InFlight(),
        nothing_run(manifest),
        pool=Pool(16, max_samples=200),
        budget=28,
    )

    assert [worker.max_samples for worker in spawns(result)] == [200] * 10


def test_a_budget_smaller_than_the_fleet_still_spawns_one_sample_each() -> None:
    manifest = synth_manifest([SynthTask("sweep", args={"n": n}) for n in range(8)])

    result = reconcile(
        manifest, InFlight(), nothing_run(manifest), pool=Pool(16), budget=3
    )

    assert [worker.max_samples for worker in spawns(result)] == [1] * 8


NUMBERING: list[tuple[str, list[int], int, int]] = [
    ("nothing has been tried", [], 0, 1),
    ("two attempts left logs", [1, 2], 2, 3),
    # the case log history alone cannot see. Without it every attempt is
    # numbered 1, and since the number names the worker, each respawn writes
    # over the last one's in-flight entry -- so the record cannot count them
    # either, and the stall guard below never fires
    ("two died before landing one", [], 2, 3),
    ("one left a log and one did not", [1], 2, 3),
    # a spent attempt that landed a log is in both counts, so the larger is the
    # answer and adding them would skip a number every time
    ("the record was lost", [1, 2], 0, 3),
]


@pytest.mark.parametrize(
    ("finished", "spent", "expected"),
    [(finished, spent, expected) for _, finished, spent, expected in NUMBERING],
    ids=[case for case, _, _, _ in NUMBERING],
)
def test_the_attempt_number_counts_every_try_steward_knows_about(
    finished: list[int], spent: int, expected: int, tmp_path: Path
) -> None:
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, finished)
    inflight = InFlight(spent={TASK.identifier: crashes(spent)})

    result = reconcile(
        manifest,
        inflight,
        observe_tasks(manifest, observe_logs(tmp_path)),
        # patience out of the way: the subject here is the number, and a task
        # with two fruitless attempts would otherwise be left alone
        pool=Pool(max_workers=8, stall_after=99),
    )

    assert planned(result)[0].attempt == expected


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


@pytest.mark.parametrize(
    ("finished", "stalled"),
    [
        # the signal is progress, not attempt count: a task getting further
        # each time is converging however many attempts that takes
        pytest.param([1], False, id="one_attempt_that_got_somewhere"),
        pytest.param([0], False, id="one_attempt_that_got_nowhere"),
        pytest.param([1, 2, 3, 4, 5], False, id="climbing_is_never_stalled"),
        pytest.param([4, 4], False, id="one_repeat_is_ordinary"),
        pytest.param([4, 4, 4], True, id="two_repeats_is_a_pattern"),
        pytest.param([0, 0], True, id="two_attempts_that_finished_nothing"),
        # a run of failures the task then recovers from starts the count over
        pytest.param([4, 4, 5], False, id="progress_resets_the_run"),
        pytest.param([4, 4, 5, 5, 5], True, id="and_it_can_stall_again_after"),
        # going backwards is not progress, however large the earlier number:
        # a resume that reused nothing has accomplished nothing
        pytest.param([9, 1, 1], True, id="regression_counts_as_fruitless"),
    ],
)
def test_a_task_stops_being_respawned_once_it_stops_getting_anywhere(
    finished: list[int], stalled: bool, tmp_path: Path
) -> None:
    """`SpawnWorker` is the one action here that is not convergent.

    Nothing else in this vocabulary can repeat forever. Without this a task
    that fails identically every time is respawned every ten minutes until
    somebody notices, which is precisely what a task with permanently-failing
    samples does: `SHORT` forever, resumed forever, finishing nothing new.
    """
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, finished)

    result = reconcile(
        manifest, InFlight(), observe_tasks(manifest, observe_logs(tmp_path)), pool=POOL
    )

    assert (spawns(result) == []) == stalled
    assert result.summary.stalled == ([TASK.identifier] if stalled else [])
    # a stalled task is not queued either -- it is not waiting for a slot,
    # it is waiting for a person
    assert result.queued == []


def test_the_stall_threshold_is_a_knob() -> None:
    assert DEFAULT_STALL_AFTER == 2


@pytest.mark.parametrize(
    ("stall_after", "stalled"),
    [
        pytest.param(1, True, id="impatient"),
        pytest.param(2, False, id="default"),
        pytest.param(5, False, id="patient"),
    ],
)
def test_how_much_patience_a_workspace_wants_is_settable(
    stall_after: int, stalled: bool, tmp_path: Path
) -> None:
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, [4, 4])

    result = reconcile(
        manifest,
        InFlight(),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=Pool(max_workers=8, stall_after=stall_after),
    )

    assert (spawns(result) == []) == stalled


@pytest.mark.parametrize(
    ("spent", "stalled"),
    [
        pytest.param(0, False, id="never_tried"),
        pytest.param(1, False, id="tried_once"),
        pytest.param(2, True, id="tried_twice_and_left_nothing"),
    ],
)
def test_a_worker_that_dies_before_landing_a_log_is_counted_by_the_record(
    spent: int, stalled: bool
) -> None:
    """The other half of the guard, and the one the log directory cannot see.

    A definition that will not import, or an OOM during startup, leaves nothing
    behind — so the task reads `missing` on every turn exactly as it did on the
    first, and only the in-flight record knows it has been tried at all.
    """
    manifest = synth_manifest([TASK])

    result = reconcile(
        manifest,
        InFlight(spent={TASK.identifier: crashes(spent)} if spent else {}),
        nothing_run(manifest),
        pool=POOL,
    )

    assert (spawns(result) == []) == stalled


MERGED: list[tuple[str, int, bool]] = [
    ("one crash after it", 1, False),
    ("two crashes after it", 2, True),
    ("four crashes after it", 4, True),
]


@pytest.mark.parametrize(
    ("after", "stalled"),
    [(after, stalled) for _, after, stalled in MERGED],
    ids=[case for case, _, _ in MERGED],
)
def test_crashes_after_a_partial_log_count_toward_the_same_stall(
    after: int, stalled: bool, tmp_path: Path
) -> None:
    """Evidence in both places is one history, not two.

    The guard read the record only for a task with *no* logs at all, so a task
    whose first attempt landed a partial log and whose every attempt since died
    at import looked like one attempt that made progress — and was respawned
    forever, which is the failure the guard exists to stop.
    """
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, [4])
    # the attempt that landed that log is spent too, and it started before it
    spent = crashes(1, since=9) + crashes(after)

    result = reconcile(
        manifest,
        InFlight(spent={TASK.identifier: spent}),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
    )

    assert (spawns(result) == []) == stalled


def test_crashes_before_a_working_attempt_do_not_count(tmp_path: Path) -> None:
    # the reason the record carries times rather than a count: two crashes and
    # then an attempt that got somewhere is a task recovering, and a count alone
    # cannot tell that from a task that got somewhere and then started crashing
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, [4])

    result = reconcile(
        manifest,
        InFlight(spent={TASK.identifier: crashes(2, since=8)}),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
    )

    assert len(spawns(result)) == 1


def test_a_stall_survives_the_two_timestamp_formats(tmp_path: Path) -> None:
    # a log's `created` and the record's `ts` are written by different code with
    # different offset conventions, and comparing them as strings would compare
    # `Z` against `+00:00` -- so the guard would silently stop firing
    manifest = synth_manifest([TASK])
    write_log(tmp_path, TASK, total=4, completed=4, created="2026-08-23T10:00:00Z")

    result = reconcile(
        manifest,
        InFlight(spent={TASK.identifier: crashes(2, since=11)}),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
    )

    assert spawns(result) == []


def test_an_unreadable_attempt_start_is_reported_rather_than_silently_dropped(
    tmp_path: Path,
) -> None:
    """The guard's leniency stops being invisible.

    An instant that will not parse is *not evidence* — the right refusal, since
    inventing a stall is worse than losing one — but a task whose record is
    damaged then looks exactly like one converging, and nothing said so.
    """
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, [4])

    result = reconcile(
        manifest,
        InFlight(spent={TASK.identifier: ["whenever", *crashes(1)]}),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
    )

    assert any("whenever" in warning for warning in result.warnings)
    # and the guard stayed lenient: one countable crash is not a stall
    assert len(spawns(result)) == 1


def test_an_unreadable_log_time_is_reported_rather_than_exempting_the_task(
    tmp_path: Path,
) -> None:
    # a log written by something with a different idea of a timestamp -- no
    # fixture can write one, since the header validates its own `created`, so
    # the observation is damaged by hand. The crashes cannot be ordered
    # against it and none of them count, which the warning is the only
    # account of
    manifest = synth_manifest([TASK])
    write_log(tmp_path, TASK, total=4, completed=4)
    observed = observe_tasks(manifest, observe_logs(tmp_path))
    (observation,) = observed.tasks
    assert observation.current is not None
    observed = replace(
        observed,
        tasks=[
            replace(
                observation, current=replace(observation.current, created="whenever")
            )
        ],
    )

    result = reconcile(
        manifest,
        InFlight(spent={TASK.identifier: crashes(2)}),
        observed,
        pool=POOL,
    )

    assert any(
        "whenever" in warning and "stall guard" in warning
        for warning in result.warnings
    )
    assert len(spawns(result)) == 1


def invalidate(log_dir: Path, task: SynthTask, *, at: str) -> None:
    """The newest log, marked invalidated, its file stamped when that happened.

    The stamp is what a real invalidation leaves behind — it rewrites the log —
    and it is the only record of *when* somebody asked for the re-run. Set
    explicitly rather than taken from the clock, so the tests stay a table.
    """
    written = write_log(
        log_dir,
        task,
        total=4,
        completed=4,
        invalidated=True,
        created="2026-08-23T20:00:00+00:00",
    )
    when = datetime.fromisoformat(at).timestamp()
    os.utime(written, (when, when))


def test_an_invalidation_clears_the_stall_behind_it(tmp_path: Path) -> None:
    """The guard yields to a human.

    An invalidation is a decision to try again, made by the only party entitled
    to make one, so the run of fruitless attempts before it stops counting.
    """
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, [4, 4, 4])
    invalidate(tmp_path, TASK, at="2026-08-23T21:00:00+00:00")

    result = reconcile(
        manifest,
        # crashes from before they acted, which is the history being forgiven
        InFlight(spent={TASK.identifier: crashes(3, since=18)}),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
    )

    assert len(spawns(result)) == 1
    assert result.summary.stalled == []


CLEARED: list[tuple[str, int, bool]] = [
    ("nothing has been tried since", 0, False),
    ("one retry died", 1, False),
    ("two retries died", 2, True),
]


@pytest.mark.parametrize(
    ("since", "stalled"),
    [(since, stalled) for _, since, stalled in CLEARED],
    ids=[case for case, _, _ in CLEARED],
)
def test_an_invalidation_forgives_the_past_and_not_the_future(
    since: int, stalled: bool, tmp_path: Path
) -> None:
    """Forgiveness is consumed by the retries it authorized.

    A retry that dies before landing a replacement leaves the invalidated log
    current, so an exemption that merely *returned* would never expire — and an
    import error under an invalidated log is then respawned every ten minutes
    for as long as the run lasts, which is the one branch of this guard with no
    ceiling on it at all.
    """
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, [4, 4, 4])
    invalidate(tmp_path, TASK, at="2026-08-23T21:00:00+00:00")

    result = reconcile(
        manifest,
        InFlight(spent={TASK.identifier: crashes(since, since=22)}),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
    )

    assert (spawns(result) == []) == stalled


# --- a rerun ruling's forgiveness, and its place in the queue -------------

RULED_AT = "2026-08-23T21:00:00+00:00"


def test_a_rerun_ruling_clears_the_stall_behind_it(tmp_path: Path) -> None:
    # the ruling is the decision to try again, made by the only party entitled
    # to make one -- the invalidation clause reached one layer up, and the
    # forgiveness that lets the applier's respawn actually happen
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, [4, 4, 4])
    observed = observe_tasks(manifest, observe_logs(tmp_path))

    held = reconcile(manifest, InFlight(), observed, pool=POOL)
    forgiven = reconcile(
        manifest,
        InFlight(),
        observed,
        pool=POOL,
        ruled={TASK.identifier: RULED_AT},
    )

    assert spawns(held) == []
    assert held.summary.stalled == [TASK.identifier]
    assert len(spawns(forgiven)) == 1
    assert forgiven.summary.stalled == []


@pytest.mark.parametrize(
    ("since", "stalled"),
    [(since, stalled) for _, since, stalled in CLEARED],
    ids=[case for case, _, _ in CLEARED],
)
def test_a_ruling_forgives_the_past_and_not_the_future(
    since: int, stalled: bool, tmp_path: Path
) -> None:
    # the same consumed-forgiveness property the invalidation table pins: the
    # first post-ruling attempt starts fresh, and `stall_after` fresh
    # fruitless attempts re-stall
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, [4, 4, 4])

    result = reconcile(
        manifest,
        InFlight(spent={TASK.identifier: crashes(since, since=22)}),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
        ruled={TASK.identifier: RULED_AT},
    )

    assert (spawns(result) == []) == stalled


def test_an_authorized_rerun_goes_first_by_sorting_not_preemption(
    tmp_path: Path,
) -> None:
    # a stable partition of the pending queue: authorized first, manifest
    # order preserved within each half -- one code path, no second scheduler
    ordinary, authorized = SynthTask("ordinary"), SynthTask("authorized")
    manifest = synth_manifest([ordinary, authorized])

    result = reconcile(
        manifest,
        InFlight(),
        nothing_run(manifest),
        pool=Pool(max_tasks=1),
        ruled={authorized.identifier: RULED_AT},
    )

    assert keys(planned(result)) == [manifest.tasks[1].key]
    assert [task.key for task in result.queued] == [manifest.tasks[0].key]
    assert result.summary.rerunning == 1


def test_an_invalidated_task_is_authorized_without_a_ruling(
    tmp_path: Path,
) -> None:
    # only a ruling invalidates, so INVALIDATED is read as authorization in
    # its own right -- the crash-recovery case where the ruling's window has
    # already resolved but the respawn has not happened yet
    fresh, reopened = SynthTask("fresh"), SynthTask("reopened")
    manifest = synth_manifest([fresh, reopened])
    invalidate(tmp_path, reopened, at=RULED_AT)

    result = reconcile(
        manifest,
        InFlight(),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
    )

    assert keys(planned(result))[0] == manifest.tasks[1].key
    assert result.summary.rerunning == 1


def test_a_stalled_task_frees_the_slot_it_was_holding(tmp_path: Path) -> None:
    healthy, stuck = SynthTask("healthy"), SynthTask("stuck")
    manifest = synth_manifest([stuck, healthy])
    attempts(tmp_path, stuck, [4, 4, 4])

    result = reconcile(
        manifest,
        InFlight(),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=Pool(max_workers=1),
    )

    # the stuck task is first in the manifest, so without the guard it would
    # take the only slot every turn and the healthy one would never run
    assert keys(planned(result)) == [manifest.tasks[1].key]


def test_an_accepted_task_is_neither_spawned_nor_stalled(tmp_path: Path) -> None:
    """A person ended this task, so Steward stops trying — and stops reporting.

    Both halves matter. Respawning would overrule the only party entitled to
    end it, and reporting it *stalled* would put the decision back in front of
    the person who just made it: a stall says somebody should look, which is
    exactly the question an acceptance answers.
    """
    manifest = synth_manifest([TASK])
    write_log(tmp_path, TASK, total=6)
    observed = observe_tasks(manifest, observe_logs(tmp_path))

    respawning = reconcile(manifest, InFlight(), observed, pool=POOL)
    latched = reconcile(
        manifest, InFlight(), observed, pool=POOL, accepted={TASK.identifier}
    )

    assert keys(planned(respawning)) == [manifest.tasks[0].key]
    assert planned(latched) == []
    assert latched.summary.stalled == []
    assert latched.summary.accepted == [TASK.identifier]


def test_an_accepted_task_that_would_have_stalled_reports_the_decision_instead(
    tmp_path: Path,
) -> None:
    # the guard and the latch answer the same question, so only one may speak
    manifest = synth_manifest([TASK])
    attempts(tmp_path, TASK, [4, 4, 4])
    observed = observe_tasks(manifest, observe_logs(tmp_path))

    guarded = reconcile(manifest, InFlight(), observed, pool=POOL)
    latched = reconcile(
        manifest, InFlight(), observed, pool=POOL, accepted={TASK.identifier}
    )

    assert guarded.summary.stalled == [TASK.identifier]
    assert latched.summary.stalled == []
    assert latched.summary.accepted == [TASK.identifier]


def test_an_accepted_task_with_a_live_worker_is_left_entirely_alone(
    tmp_path: Path,
) -> None:
    # an acceptance never kills a worker: stopping a process is not a
    # mechanical act, and one a minute from finishing should finish
    manifest = synth_manifest([TASK])
    write_log(tmp_path, TASK, total=6)

    result = reconcile(
        manifest,
        InFlight(running=[live(TASK.identifier)]),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
        accepted={TASK.identifier},
    )

    assert planned(result) == []
    assert result.summary.accepted == []
    assert result.summary.running == 1


def test_the_accepted_list_never_names_a_complete_task(tmp_path: Path) -> None:
    """The invariant the signoff gate's arithmetic rests on.

    It adds complete and accepted to decide the run is settled, which is only
    sound while the two cannot overlap — and they cannot, because this list is
    built inside a loop that only ever sees tasks needing work.
    """
    manifest = synth_manifest([TASK])
    write_log(tmp_path, TASK)

    result = reconcile(
        manifest,
        InFlight(),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
        accepted={TASK.identifier},
    )

    assert result.summary.states[TaskState.COMPLETE.value] == 1
    assert result.summary.accepted == []


def test_a_worker_waiting_on_a_person_is_neither_reaped_nor_replaced(
    tmp_path: Path,
) -> None:
    """A parked worker holds its slot, and that is the intended behaviour.

    Its process is alive, so it lands in `running`, occupies a slot and
    suppresses a respawn; the stall guard is consulted only for a task that
    needs spawning, which a running one does not. Nothing here knows what a
    park *is* — which is the point, and why this is worth pinning: a refactor
    that taught the guard to look at how long a running task has been quiet
    would kill the one worker somebody is on their way to answer.

    The consequence is load-bearing rather than incidental: enough parked
    workers stall a fleet, and at the ceiling they stop it, which is exactly
    what the verdict reports.
    """
    healthy, waiting = SynthTask("healthy"), SynthTask("waiting")
    manifest = synth_manifest([waiting, healthy])
    # the log a parked worker leaves: an attempt that started and got nowhere,
    # several times over -- enough to trip the guard if anything asked it
    attempts(tmp_path, waiting, [4, 4, 4])
    holding = live(manifest.tasks[0].identifier)

    result = reconcile(
        manifest,
        InFlight(running=[holding]),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=Pool(max_workers=1),
    )

    assert result.actions == []
    assert result.summary.running == 1
    assert result.summary.stalled == []
    # and the slot it is holding is genuinely held: the healthy task waits
    assert keys(planned(result)) == []
    assert keys(result.queued) == [manifest.tasks[1].key]


def test_every_attempt_of_an_orphan_is_archived(tmp_path: Path) -> None:
    """All of them, not only the current one.

    The identifier has left the definition entirely, so there is no attempt
    history left to reason about — and leaving the superseded ones behind
    would defeat the point, which is that `logs/` holds exactly what the
    current definition produced.
    """
    removed = SynthTask("removed")
    manifest = synth_manifest([TASK])
    write_log(tmp_path, removed, error="one", created="2026-08-23T18:00:00+00:00")
    write_log(tmp_path, removed, created="2026-08-23T20:00:00+00:00")

    result = reconcile(
        manifest, InFlight(), observe_tasks(manifest, observe_logs(tmp_path)), pool=POOL
    )

    assert {action.identifier for action in archives(result)} == {removed.identifier}
    assert len(archives(result)) == 2
    assert result.summary.archiving == 2


def test_an_orphan_with_a_live_worker_is_left_entirely_alone(tmp_path: Path) -> None:
    # stopping a worker is not a mechanical act, and archiving a log that is
    # still being written would take it out from under one
    removed = SynthTask("removed")
    manifest = synth_manifest([TASK])
    write_log(tmp_path, removed, status="started")

    result = reconcile(
        manifest,
        InFlight(running=[live(removed.identifier)]),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
    )

    assert archives(result) == []
    assert result.summary.orphans_running == [removed.identifier]
    assert result.summary.archiving == 0


def test_actions_are_ordered_reap_then_archive_then_spawn(tmp_path: Path) -> None:
    # reaping frees a slot the spawn may want, and archiving clears the
    # directory before anything new lands in it
    manifest = synth_manifest([TASK])
    write_log(tmp_path, SynthTask("removed"))

    result = reconcile(
        manifest,
        InFlight(departed=[dead(TASK.identifier)]),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
    )

    assert [type(action).__name__ for action in result.actions] == [
        "ReapWorker",
        "ArchiveLog",
        "SpawnWorker",
    ]


def test_paused_archives_nothing(tmp_path: Path) -> None:
    # pausing means make no changes to the run, and a move is a change
    manifest = synth_manifest([TASK])
    write_log(tmp_path, SynthTask("removed"))

    result = reconcile(
        manifest,
        InFlight(),
        observe_tasks(manifest, observe_logs(tmp_path)),
        pool=POOL,
        paused=True,
    )

    assert archives(result) == []
    assert result.summary.orphans != []


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
        InFlight(running=[live(running.identifier, pid=1)]),
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


# --- fleet width: the definition's word, not the file's -------------------


WIDTH: list[tuple[str, dict[str, Any], Pool, int | None]] = [
    ("nobody expressed one", {}, POOL, None),
    ("the definition did", {"max_tasks": 6}, POOL, 6),
    ("the command line did", {}, Pool(max_tasks=3), 3),
    ("the command line outranks it", {"max_tasks": 6}, Pool(max_tasks=3), 3),
]


@pytest.mark.parametrize(
    ("options", "pool", "expected"),
    [(options, pool, expected) for _, options, pool, expected in WIDTH],
    ids=[case for case, _, _, _ in WIDTH],
)
def test_max_tasks(options: dict[str, Any], pool: Pool, expected: int | None) -> None:
    # `max_tasks` is inspect's word, so the definition owns it and `_steward.yaml`
    # refuses it -- the chain is the command line, then the definition, then
    # unbounded
    manifest = synth_manifest([TASK], **options)

    assert resolve_max_tasks(manifest, pool) == expected


def test_an_unset_width_runs_everything_rather_than_one_at_a_time() -> None:
    # the deliberate divergence from `eval()`, whose own rule for an unset
    # max_tasks is sequential: a fleet exists to run wide, and a definition
    # that says nothing has expressed no preference rather than a preference
    manifest = synth_manifest([TASK, SynthTask("other")])

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=POOL)

    assert result.summary.spawning == 2
    assert result.summary.max_tasks is None


def test_the_definitions_width_bounds_what_starts() -> None:
    tasks = [TASK, SynthTask("second"), SynthTask("third")]
    manifest = synth_manifest(tasks, max_tasks=2)

    result = reconcile(manifest, InFlight(), nothing_run(manifest), pool=POOL)

    assert result.summary.spawning == 2
    assert len(result.queued) == 1
    assert result.summary.max_tasks == 2


# --- the ramp: on by default, pinned by anyone explicit -------------------


@pytest.mark.parametrize(
    ("options", "pool", "expected"),
    [
        # nobody said anything: the default range, whose floor is the start
        pytest.param({}, POOL, DEFAULT_SAMPLES_RAMP, id="on_by_default"),
        # an explicit max_samples anywhere pins the setpoint and switches the
        # policy off entirely -- which is what keeps `samples_ramp` from ever
        # contradicting a definition
        pytest.param({"max_samples": 60}, POOL, None, id="the_definition_pins"),
        pytest.param({}, Pool(max_samples=12), None, id="the_operator_pins"),
        pytest.param({}, Pool(samples_ramp=False), None, id="switched_off"),
        pytest.param({}, Pool(samples_ramp=(60, 300)), (60, 300), id="a_written_range"),
        pytest.param(
            {"max_samples": 60},
            Pool(samples_ramp=(60, 300)),
            None,
            id="a_pin_beats_a_range",
        ),
    ],
)
def test_samples_ramp(
    options: dict[str, Any], pool: Pool, expected: tuple[int, int] | None
) -> None:
    manifest = synth_manifest([TASK], **options)

    assert resolve_samples_ramp(manifest, pool) == expected


def test_the_ramp_floor_is_where_a_task_starts() -> None:
    manifest = synth_manifest([TASK])

    result = reconcile(
        manifest, InFlight(), nothing_run(manifest), pool=Pool(samples_ramp=(60, 300))
    )

    assert spawns(result)[0].max_samples == 60


def test_ramping_off_pins_the_default() -> None:
    manifest = synth_manifest([TASK])

    result = reconcile(
        manifest, InFlight(), nothing_run(manifest), pool=Pool(samples_ramp=False)
    )

    assert spawns(result)[0].max_samples == DEFAULT_MAX_SAMPLES


def test_a_respawn_starts_where_the_climb_left_off() -> None:
    # the climb was earned against measured headroom, and a worker crash does
    # not unmeasure it
    manifest = synth_manifest([TASK])
    identifier = manifest.tasks[0].identifier

    result = reconcile(
        manifest,
        InFlight(),
        nothing_run(manifest),
        pool=POOL,
        levels={identifier: 120},
    )

    assert spawns(result)[0].max_samples == 120


def test_a_replayed_level_is_clamped_to_the_range_in_force_now() -> None:
    # the journal says what was authorized when the climb happened and
    # `_steward.yaml` says what is authorized now; a spawn answers to the second
    manifest = synth_manifest([TASK])
    identifier = manifest.tasks[0].identifier

    result = reconcile(
        manifest,
        InFlight(),
        nothing_run(manifest),
        pool=Pool(samples_ramp=(40, 100)),
        levels={identifier: 200},
    )

    assert spawns(result)[0].max_samples == 100


def test_a_pinned_run_ignores_stale_levels() -> None:
    # levels are the ramp's own record, and a pinned setpoint is not the ramp's
    manifest = synth_manifest([TASK])
    identifier = manifest.tasks[0].identifier

    result = reconcile(
        manifest,
        InFlight(),
        nothing_run(manifest),
        pool=Pool(max_samples=12),
        levels={identifier: 120},
    )

    assert spawns(result)[0].max_samples == 12


def test_a_packed_batch_spawns_at_its_lowest_level() -> None:
    # one selection value applied per task: a fresh task must not inherit a
    # sibling's climb, and the climbed one costs a tend before the loop
    # re-raises it
    other = SynthTask("other", samples=10, epochs=1)
    manifest = synth_manifest([TASK, other])
    climbed = manifest.tasks[0].identifier

    result = reconcile(
        manifest,
        InFlight(),
        nothing_run(manifest),
        pool=Pool(max_workers=1),
        levels={climbed: 120},
    )

    (worker,) = spawns(result)
    assert len(worker.tasks) == 2
    assert worker.max_samples == DEFAULT_MAX_SAMPLES
