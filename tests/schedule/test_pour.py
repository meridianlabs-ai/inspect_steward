"""Dividing the tasks that can start now among the processes that will run them.

A table over the two knobs, because that is all the pour is: `max_tasks` decides
how much starts and `max_workers` decides how few processes it is divided into,
and every interesting case is a pair of them against a count of pending work.
"""

import pytest
from inspect_steward._schedule import Blocked, Pool, SpawnTask, pour


def pending(count: int) -> list[SpawnTask]:
    """Tasks needing work, named so the deal can be read off the result."""
    return [
        SpawnTask(
            identifier=f"id-{n}", key=f"t{n}", resume=None, attempt=1, reason=None
        )
        for n in range(count)
    ]


def sizes(placed: list[tuple[SpawnTask, ...]]) -> list[int]:
    return [len(batch) for batch in placed]


@pytest.mark.parametrize(
    ("case", "pool", "count", "running", "workers", "expected", "queued", "blocked"),
    [
        pytest.param(
            "nobody shaped the run",
            Pool(),
            5,
            0,
            0,
            [1, 1, 1, 1, 1],
            0,
            None,
            id="unshaped",
        ),
        pytest.param(
            "max_tasks alone is the old ceiling: three start, two wait",
            Pool(max_tasks=3),
            5,
            0,
            0,
            [1, 1, 1],
            2,
            Blocked.MAX_TASKS,
            id="max_tasks_alone",
        ),
        pytest.param(
            "max_workers alone never queues: five tasks poured into two processes",
            Pool(max_workers=2),
            5,
            0,
            0,
            [3, 2],
            0,
            None,
            id="max_workers_alone",
        ),
        pytest.param(
            "run it whole",
            Pool(max_workers=1),
            5,
            0,
            0,
            [5],
            0,
            None,
            id="one_process",
        ),
        pytest.param(
            "both: four may start, into two processes",
            Pool(max_workers=2, max_tasks=4),
            5,
            0,
            0,
            [2, 2],
            1,
            Blocked.MAX_TASKS,
            id="both",
        ),
        pytest.param(
            "fewer pending than processes allowed leaves processes unused",
            Pool(max_workers=8),
            3,
            0,
            0,
            [1, 1, 1],
            0,
            None,
            id="fewer_pending_than_processes",
        ),
        pytest.param(
            "tasks already in flight count against max_tasks",
            Pool(max_tasks=4),
            5,
            3,
            3,
            [1],
            4,
            Blocked.MAX_TASKS,
            id="running_tasks_counted",
        ),
        pytest.param(
            "processes already alive count against max_workers",
            Pool(max_workers=4),
            6,
            2,
            2,
            [3, 3],
            0,
            None,
            id="running_workers_counted",
        ),
        pytest.param(
            "a full task budget places nothing",
            Pool(max_tasks=3),
            5,
            3,
            3,
            [],
            5,
            Blocked.MAX_TASKS,
            id="task_budget_full",
        ),
        pytest.param(
            "a full process budget places nothing, and queues what it cannot host",
            Pool(max_workers=2),
            5,
            2,
            2,
            [],
            5,
            Blocked.MAX_WORKERS,
            id="process_budget_full",
        ),
        pytest.param(
            "nothing pending",
            Pool(),
            0,
            0,
            0,
            [],
            0,
            None,
            id="nothing_pending",
        ),
    ],
)
def test_the_pour(
    case: str,
    pool: Pool,
    count: int,
    running: int,
    workers: int,
    expected: list[int],
    queued: int,
    blocked: Blocked | None,
) -> None:
    poured = pour(
        pending(count),
        pool=pool,
        max_tasks=pool.max_tasks,
        tasks_running=running,
        workers_running=workers,
    )

    assert sizes(poured.workers) == expected, case
    assert len(poured.queued) == queued, case
    # every pending task is placed or queued, never both and never neither
    assert sum(sizes(poured.workers)) + len(poured.queued) == count, case
    # and the bound named is the one a reader could usefully raise
    assert poured.blocked == blocked, case


def test_a_full_process_budget_queues_rather_than_joining_a_live_worker() -> None:
    # a selection document is written once, at spawn, so a running worker
    # cannot be handed more work -- the tasks wait for a process to free up
    poured = pour(
        pending(4),
        pool=Pool(max_workers=2),
        max_tasks=None,
        tasks_running=2,
        workers_running=2,
    )

    assert poured.workers == []
    assert [task.identifier for task in poured.queued] == [f"id-{n}" for n in range(4)]


def test_tasks_are_dealt_round_robin_rather_than_sliced() -> None:
    """Spawn order is task-major, so a contiguous slice is the wrong cut.

    `_spawn_order` transposes the enumeration so that consecutive entries are the
    same task across models. Slicing would put every model of one task in one
    process, and losing that process would cost the task on every arm at once —
    the uncomparable interruption the transposition exists to prevent.
    """
    poured = pour(
        pending(6),
        pool=Pool(max_workers=2),
        max_tasks=None,
        tasks_running=0,
        workers_running=0,
    )

    assert [[task.identifier for task in batch] for batch in poured.workers] == [
        ["id-0", "id-2", "id-4"],
        ["id-1", "id-3", "id-5"],
    ]


def test_a_run_short_of_processes_blames_the_process_limit_not_the_task_one() -> None:
    """Both keys set, one binding — and naming the wrong one sends a reader to a number that changes nothing.

    Nine of ten task slots are free, so `max_tasks` is nowhere near holding this back; the single process the run is allowed is already alive. Raising `max_tasks` to a hundred would start exactly as much work as raising it to eleven, which is none.
    """
    poured = pour(
        pending(4),
        pool=Pool(max_workers=1),
        max_tasks=10,
        tasks_running=1,
        workers_running=1,
    )

    assert poured.workers == []
    assert poured.blocked is Blocked.MAX_WORKERS


def test_the_queue_keeps_spawn_order() -> None:
    # the queue is the same decision deferred, so what waits is the tail of the
    # order rather than an arbitrary remainder
    poured = pour(
        pending(5),
        pool=Pool(),
        max_tasks=2,
        tasks_running=0,
        workers_running=0,
    )

    assert [task.identifier for task in poured.queued] == ["id-2", "id-3", "id-4"]
