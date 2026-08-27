"""Rows about samples, assembled from a log directory and a live fleet.

The rows are what answers *how is the run going*, and the interesting part is
that a row is filled from two places at once — the log for the denominators and
the worker for everything that moves — while producing one shape either way. So
the cases here are mostly about which source wins, and about what a column says
when it has nothing behind it.

No processes: a `LiveFleet` is a dataclass, which is exactly why `read_fleet`
returns one rather than reaching into the table itself.
"""

import os
from pathlib import Path

import pytest
from inspect_steward._evalset.observe import TaskState, observe_logs, observe_tasks
from inspect_steward._tend import Progress, progress_table, task_progress
from inspect_steward._tend.progress import LIVE_ONLY, live_totals
from inspect_steward._worker import (
    LiveConnections,
    LiveFleet,
    LiveSamples,
    LiveTask,
    LiveUsage,
)

from .._logs import SynthTask, synth_manifest, write_log

TASK = SynthTask("probe", samples=10, epochs=1)


def rows(log_dir: Path, tasks: list[SynthTask], fleet: LiveFleet) -> Progress:
    manifest = synth_manifest(tasks)
    observed = observe_tasks(manifest, observe_logs(log_dir))
    return Progress(rows=task_progress(observed, fleet))


def live(
    task: SynthTask,
    *,
    completed: int = 0,
    total: int = 10,
    in_flight: int = 0,
    queued: int = 0,
    turns: int = 0,
    messages: int = 0,
    connections: tuple[int, int | None] = (0, None),
    unavailable: str | None = None,
) -> LiveFleet:
    in_use, limit = connections
    return LiveFleet(
        tasks={
            task.identifier: LiveTask(
                pid=1,
                identifier=task.identifier,
                samples=LiveSamples(
                    total=total, completed=completed, in_flight=in_flight, queued=queued
                ),
                usage=LiveUsage(turns=turns, messages=messages),
                connections=LiveConnections(in_use=in_use, limit=limit),
                unavailable=unavailable,
            )
        }
    )


def test_a_task_that_has_never_run_still_knows_how_big_it_is(tmp_path: Path) -> None:
    # the manifest carries samples × epochs, so the denominator exists before
    # there is any log to read it from -- which is what lets a fresh run report
    # 0/400 rather than 0/0
    progress = rows(tmp_path, [TASK], LiveFleet())

    (row,) = progress.rows
    assert (row.completed, row.total) == (0, 10)
    assert row.state == TaskState.MISSING
    assert row.live is False


def test_a_settled_task_is_described_entirely_by_its_log(tmp_path: Path) -> None:
    write_log(tmp_path, TASK, total=10, completed=7)

    (row,) = rows(tmp_path, [TASK], LiveFleet()).rows

    assert (row.completed, row.total, row.errored) == (7, 10, 3)
    assert (row.running, row.queued) == (0, 0)
    assert row.connections is None


def test_a_running_worker_outranks_its_own_log(tmp_path: Path) -> None:
    # a log is written behind the run -- buffered, and flushed on a policy -- so
    # the process's own count is the fresher of the two wherever both exist
    write_log(tmp_path, TASK, status="started", total=10, completed=2)

    (row,) = rows(
        tmp_path, [TASK], live(TASK, completed=6, total=10, in_flight=3, queued=1)
    ).rows

    assert (row.completed, row.total) == (6, 10)
    assert (row.running, row.queued) == (3, 1)
    assert row.live is True


def test_a_worker_that_did_not_answer_falls_back_to_the_log(tmp_path: Path) -> None:
    # busy is the ordinary case, not a fault: the control server shares the
    # eval's event loop, so a fleet mid-generate is a fleet that may not reply.
    # The log is a prior attempt's -- a `started` one carries no results at all,
    # which is itself why a running row prefers the worker
    write_log(tmp_path, TASK, status="error", error="boom", total=10, completed=2)

    (row,) = rows(tmp_path, [TASK], live(TASK, unavailable="busy")).rows

    assert (row.completed, row.total) == (2, 10)
    assert row.live is False
    assert row.unavailable == "busy"
    assert row.budget is None


BUDGETS: list[tuple[str, dict[str, int], int, int, str]] = [
    ("only a message limit", {"message_limit": 30}, 0, 11, "m"),
    ("only a turn limit", {"turn_limit": 50}, 12, 0, "t"),
]


@pytest.mark.parametrize(
    ("limits", "turns", "messages", "suffix"),
    [
        (limits, turns, messages, suffix)
        for _, limits, turns, messages, suffix in BUDGETS
    ],
    ids=[case for case, _, _, _, _ in BUDGETS],
)
def test_the_budget_column_reports_whichever_limit_is_in_force(
    limits: dict[str, int], turns: int, messages: int, suffix: str, tmp_path: Path
) -> None:
    task = SynthTask("probe", samples=10, limits=limits)
    write_log(tmp_path, task, status="started")

    (row,) = rows(
        tmp_path, [task], live(task, in_flight=1, turns=turns, messages=messages)
    ).rows

    assert row.budget is not None
    assert row.budget.suffix == suffix
    assert row.budget.used == max(turns, messages)


def test_the_budget_shown_is_the_one_closest_to_stopping_a_sample(
    tmp_path: Path,
) -> None:
    # a task declaring two limits is not asking a reader to pick, and the one
    # that matters is whichever will be reached first
    task = SynthTask(
        "probe", samples=10, limits={"message_limit": 100, "turn_limit": 20}
    )
    write_log(tmp_path, task, status="started")

    (row,) = rows(tmp_path, [task], live(task, in_flight=1, messages=10, turns=18)).rows

    assert row.budget is not None
    assert (row.budget.name, row.budget.used, row.budget.limit) == ("turns", 18, 20)


def test_a_settled_task_shows_no_budget_at_all(tmp_path: Path) -> None:
    # usage comes from the worker, so a finished task has a limit and nothing to
    # put against it -- and `0/30` would say *spent none of thirty*, which is a
    # claim rather than a gap
    task = SynthTask("probe", samples=10, limits={"message_limit": 30})
    write_log(tmp_path, task)

    (row,) = rows(tmp_path, [task], LiveFleet()).rows

    assert row.budget is None


def test_the_headline_metric_says_which_metric_it_is(tmp_path: Path) -> None:
    # nothing in a log marks a metric as primary, so the column is a convention
    # and a reader who cannot see which one was picked cannot tell it from a
    # guess (roadmap.md §5, item 14)
    write_log(tmp_path, TASK, scores={"exact": {"accuracy": 0.75}})

    (row,) = rows(tmp_path, [TASK], LiveFleet()).rows

    assert row.headline == 0.75
    assert row.headline_name == "exact/accuracy"


def test_totals_are_samples_rather_than_tasks(tmp_path: Path) -> None:
    # the whole point of the table: four tasks is four rows either way, and what
    # differs is whether the run reports "2 incomplete" or "37/502, 20%"
    other = SynthTask("other", samples=6)
    write_log(tmp_path, TASK, total=10, completed=4)
    write_log(tmp_path, other, total=6, completed=6)

    progress = rows(tmp_path, [TASK, other], LiveFleet())

    assert (progress.completed, progress.total) == (10, 16)
    assert round(progress.fraction * 100) == 62


def test_an_empty_run_renders_no_table(tmp_path: Path) -> None:
    assert progress_table(Progress()) == []


def test_a_column_nothing_has_to_say_is_dropped(tmp_path: Path) -> None:
    write_log(tmp_path, TASK, total=10, completed=10)

    (line, *_) = progress_table(rows(tmp_path, [TASK], LiveFleet()))

    # nothing is running, so no running, queued, connection, or budget column
    # holds its width open -- the row is the key and the two counts, full stop
    assert line.split()[-2:] == ["10/10", "100%"]
    assert line == line.rstrip()


def test_a_live_row_carries_every_column(tmp_path: Path) -> None:
    task = SynthTask("probe", samples=10, limits={"turn_limit": 300})
    write_log(tmp_path, task, status="started", total=10, completed=1)

    (line, *_) = progress_table(
        rows(
            tmp_path,
            [task],
            live(
                task,
                completed=5,
                total=123,
                in_flight=57,
                queued=61,
                turns=115,
                connections=(52, 80),
            ),
        )
    )

    for cell in ("5/123", "4%", "57r", "61q", "52/80c", "115/300t"):
        assert cell in line, f"{cell!r} missing from {line!r}"


def test_the_totals_appear_only_when_there_is_more_than_one_row(
    tmp_path: Path,
) -> None:
    # summing one row's samples restates the row
    write_log(tmp_path, TASK)
    (_, footer) = progress_table(rows(tmp_path, [TASK], LiveFleet()))
    assert "samples" not in footer

    other = SynthTask("other", samples=6)
    write_log(tmp_path, other)
    lines = progress_table(rows(tmp_path, [TASK, other], LiveFleet()))
    assert len(lines) == 3
    assert "16/16 samples" in lines[-1]


def test_one_task_still_says_which_model_it_ran(tmp_path: Path) -> None:
    # the model is elided from a key that has nobody to be compared against,
    # so the footer is the only place left for it -- and a row that names
    # neither has lost it outright
    write_log(tmp_path, TASK)

    lines = progress_table(rows(tmp_path, [TASK], LiveFleet()))

    assert "mockllm/model" not in lines[0]
    assert lines[-1].strip() == "mockllm/model"


def test_no_footer_when_there_is_nothing_to_put_in_one(tmp_path: Path) -> None:
    # one row whose model is on the row already: no shared fact, no totals
    orphan = SynthTask("gone")
    write_log(tmp_path, orphan)

    lines = progress_table(rows(tmp_path, [], LiveFleet()))

    assert len(lines) == 1
    assert lines[0].startswith("⌫ ")


SLACK = 76
"""Columns a Slack code block holds before it wraps.

The narrowest surface the table has to survive, and the reason display keys are
shortened against the rows on screen rather than printed as the manifest
computed them. Step 24 sends the post; this is where the table is held to
fitting in one.
"""


def test_a_busy_sweep_fits_the_narrowest_surface(tmp_path: Path) -> None:
    # every column present at once, on keys the length real benchmarks have --
    # which is the worst case, since a settled campaign drops most of them
    tasks = [
        SynthTask(name, samples=300, limits={"turn_limit": 300})
        for name in ("sec_bench_pro", "exploit_gym_userspace", "oss_fuzz_t300")
    ]
    for task in tasks:
        write_log(tmp_path, task, status="started", total=300, completed=1)

    fleet = LiveFleet(
        tasks={
            task.identifier: live(
                task,
                completed=37,
                total=183,
                in_flight=83,
                queued=63,
                turns=115,
                connections=(52, 80),
            ).tasks[task.identifier]
            for task in tasks
        }
    )
    lines = progress_table(rows(tmp_path, tasks, fleet))

    for line in lines:
        assert len(line) <= SLACK, f"{len(line)} columns: {line!r}"
    # and it is not fitting by having thrown the numbers away
    assert "52/80c" in lines[0] and "115/300t" in lines[0]


def test_the_shared_model_is_named_once_rather_than_on_every_row(
    tmp_path: Path,
) -> None:
    other = SynthTask("other", samples=6)
    write_log(tmp_path, TASK, total=10, completed=10)
    write_log(tmp_path, other, total=6, completed=6)

    lines = progress_table(rows(tmp_path, [TASK, other], LiveFleet()))

    assert lines[0].startswith("✓ probe ")
    assert "mockllm/model" not in lines[0]
    assert "mockllm/model" in lines[-1]


def test_a_shortened_key_uses_the_name_the_manifest_shows(tmp_path: Path) -> None:
    # `compute_display_keys` builds the full key from `display_name or name`,
    # and shortening has to agree with it -- a task the whole run calls
    # `Friendly Name` must not become the internal name in one column
    named = SynthTask("internal_name", display_name="Friendly Name")
    write_log(tmp_path, named)

    (line, *_) = progress_table(rows(tmp_path, [named], LiveFleet()))

    assert "Friendly Name" in line
    assert "internal_name" not in line


# --- the live block -----------------------------------------------------


def test_the_block_is_per_task_for_tallies_and_per_process_for_cost() -> None:
    """The split that decides how each figure is summed.

    A packed worker reports a row per task and every one names the same
    process. Refusals really are per task and add up; memory is not, and adding
    it up would multiply one process's resident set by its batch size.
    """
    packed = LiveFleet(
        tasks={
            f"task-{index}": LiveTask(
                pid=os.getpid(),
                identifier=f"task-{index}",
                refusals=index,
                http_retries=index * 2,
            )
            for index in range(4)
        }
    )

    block = live_totals(packed)

    assert block is not None
    assert block.tasks == 4
    assert block.usage.processes == 1
    assert (block.refusals, block.http_retries) == (6, 12)


def test_a_busy_worker_costs_its_tallies_and_not_its_memory() -> None:
    """The two figures come from different places and fail independently.

    A refusal count comes from the socket the worker is too busy to serve, so
    it is *absent* rather than zero. Its resident set comes from the kernel,
    which answers regardless — and a fleet where nothing answered used to
    render as *nothing is running*, falling back to a startup projection while
    real processes held real memory.
    """
    busy = LiveFleet(
        tasks={
            "task-1": LiveTask(pid=os.getpid(), identifier="task-1", unavailable="busy")
        }
    )

    block = live_totals(busy, [os.getpid()])

    assert block is not None
    assert (block.tasks, block.unavailable) == (0, 1)
    assert block.usage.processes == 1
    # no false zeros: with nothing answering there is no refusal count to show
    assert "refusals" not in block.figures
    assert "1 not answering" in block.figures


def test_nothing_running_is_no_block_at_all() -> None:
    # absent rather than zeroed, which is what lets the renderer put the
    # capture's startup bound in its place
    assert live_totals(LiveFleet()) is None
    assert live_totals(LiveFleet(), []) is None


def test_the_block_says_what_it_covers_and_that_it_is_only_now() -> None:
    # a figure that *falls* as tasks complete reads as a problem fixing itself,
    # so the denominator and the caveat are part of the block rather than
    # something a renderer might or might not add
    block = live_totals(
        LiveFleet(
            tasks={"task-1": LiveTask(pid=os.getpid(), identifier="task-1", refusals=3)}
        )
    )

    assert block is not None
    assert block.figures.startswith("1 task · 3 refusals · 0 HTTP retries")
    assert "average since start" in block.figures
    assert "fall as tasks finish" in LIVE_ONLY
