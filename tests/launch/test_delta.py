"""What a launch would change, decided against directories nothing ran.

The delta is pure, so every row is reachable from two manifests and two log
directories — and both are cheap: `_logs.py` writes a synthetic log in under a
kilobyte, and it derives the manifest row and the log from one `EvalSpec`, so
the identifiers match by construction rather than by a literal repeated twice.

**Read through `observe_logs` rather than assembled by hand.** Building
`ObservedLogs` directly would be marginally cheaper and would couple these
cases to `LogAttempt`'s shape; going through the real reader costs a few file
writes and means a row that claims to move a log names a log something actually
found.

**No launches.** The one claim here that a real capture would add — that a
*captured* identifier matches the one a worker's log carries — is
`tests/evalset/test_selection.py`'s, and `test_tend_live.py` re-establishes it
through `launch` itself.
"""

from pathlib import Path

import pytest
from inspect_steward._evalset.observe import ObservedLogs, observe_logs
from inspect_steward._launch import Change, compute_delta
from inspect_steward._schedule import RunningWorker

from .._logs import SynthTask, synth_manifest, write_log

ADDITION = SynthTask("addition", samples=10)
ECHO = SynthTask("echo", samples=5)
ECHO_3 = SynthTask("echo", samples=5, epochs=3)
"""`echo` with the epochs raised. A *different task row* and the **same identifier**, because `task_identifier` hashes a task's execution limits and not its epochs — which is the whole reason `extend` is a row rather than a remove-and-add."""

ADDITION_SCALED = SynthTask("addition", args={"scale": 2}, samples=10)
"""`addition` with an argument. Same file, name, and model; new identifier — an edit rather than a deletion, which is exactly the pair the gate cannot tell apart on identity alone."""

OTHER_MODEL = SynthTask("addition", model="mockllm/other", samples=10)


def empty() -> ObservedLogs:
    return ObservedLogs(log_dir="nowhere")


def running(task: SynthTask, worker: str) -> RunningWorker:
    """A worker alive on this task. The pid is never signalled from here — the delta is pure — so it only has to be a number."""
    return RunningWorker(
        worker=worker, identifiers=(task.identifier,), pid=1, host="here"
    )


@pytest.mark.parametrize(
    "case,old,new,in_logs,in_archive,expected,additive",
    [
        (
            "a task the committed manifest does not name is an addition",
            [ADDITION],
            [ADDITION, ECHO],
            [ADDITION],
            [],
            {Change.ADD: 1},
            True,
        ),
        (
            "raised epochs keep the identifier and extend the task",
            [ADDITION, ECHO],
            [ADDITION, ECHO_3],
            [ADDITION, ECHO],
            [],
            {Change.EXTEND: 1},
            True,
        ),
        (
            "lowered epochs are already satisfied and are not a change",
            [ADDITION, ECHO_3],
            [ADDITION, ECHO],
            [ADDITION],
            [],
            {},
            True,
        ),
        (
            "a task gone from the definition with nothing of its name left is removed",
            [ADDITION, ECHO],
            [ADDITION],
            [ADDITION, ECHO],
            [],
            {Change.REMOVED: 1},
            False,
        ),
        (
            "an edited argument supersedes the old identifier and adds the new one",
            [ADDITION],
            [ADDITION_SCALED],
            [ADDITION],
            [],
            {Change.ADD: 1, Change.SUPERSEDED: 1},
            False,
        ),
        (
            "dropping one of two models removes rather than supersedes",
            [ADDITION, OTHER_MODEL],
            [ADDITION],
            [ADDITION, OTHER_MODEL],
            [],
            {Change.REMOVED: 1},
            False,
        ),
        (
            "a wanted task whose only log is in the archive is restored",
            [ADDITION],
            [ADDITION, ECHO],
            [ADDITION],
            [ECHO],
            {Change.ADD: 1, Change.RESTORE: 1},
            True,
        ),
        (
            "a wanted task already holding a log is not restored over",
            [ADDITION],
            [ADDITION],
            [ADDITION],
            [ADDITION],
            {},
            True,
        ),
        (
            "an unedited definition changes nothing",
            [ADDITION, ECHO],
            [ADDITION, ECHO],
            [ADDITION, ECHO],
            [],
            {},
            True,
        ),
    ],
)
def test_the_delta_rows(
    tmp_path: Path,
    case: str,
    old: list[SynthTask],
    new: list[SynthTask],
    in_logs: list[SynthTask],
    in_archive: list[SynthTask],
    expected: dict[Change, int],
    additive: bool,
) -> None:
    logs = tmp_path / "logs"
    archive = tmp_path / "logs-archive"
    for task in in_logs:
        write_log(logs, task)
    for task in in_archive:
        write_log(archive, task)

    delta = compute_delta(
        synth_manifest(new),
        synth_manifest(old),
        logs=observe_logs(str(logs)),
        archived=observe_logs(str(archive)),
        running=[],
    )

    counted = {change: len(delta.of(change)) for change in Change if delta.of(change)}
    assert counted == expected, case
    assert delta.additive is additive, case
    assert delta.empty is (not expected), case


def test_a_first_launch_adds_everything_and_needs_no_acceptance(
    tmp_path: Path,
) -> None:
    """No committed manifest is not the same as an empty one, and only one of the two is safe to treat as *everything is new*."""
    delta = compute_delta(
        synth_manifest([ADDITION, ECHO]),
        None,
        logs=empty(),
        archived=empty(),
        running=[],
    )

    assert delta.first is True
    assert len(delta.of(Change.ADD)) == 2
    assert delta.additive is True


def test_a_first_launch_still_restores_what_the_archive_holds(
    tmp_path: Path,
) -> None:
    """The case a deleted `.steward/` produces: desired state is gone, the results are not.

    Without this the archive's whole value as a cache is conditional on the one
    directory the design tells people they may delete.
    """
    archive = tmp_path / "logs-archive"
    write_log(archive, ADDITION)

    delta = compute_delta(
        synth_manifest([ADDITION, ECHO]),
        None,
        logs=empty(),
        archived=observe_logs(str(archive)),
        running=[],
    )

    assert [row.identifier for row in delta.of(Change.RESTORE)] == [ADDITION.identifier]
    assert len(delta.of(Change.ADD)) == 2


def test_an_archiving_row_names_the_logs_and_the_worker_it_would_stop(
    tmp_path: Path,
) -> None:
    """The two things the gate's consent is actually about.

    A count of tasks is not enough to decide with: what a reader is agreeing to
    is that these files move and that this worker stops mid-run.
    """
    logs = tmp_path / "logs"
    write_log(logs, ECHO, created="2026-08-23T18:00:00+00:00")
    write_log(logs, ECHO, created="2026-08-23T19:00:00+00:00")

    delta = compute_delta(
        synth_manifest([ADDITION]),
        synth_manifest([ADDITION, ECHO]),
        logs=observe_logs(str(logs)),
        archived=empty(),
        running=[running(ECHO, "echo_abc_1")],
    )

    (row,) = delta.of(Change.REMOVED)
    # every attempt, not only the current one: the identifier has left the
    # definition, so there is no attempt history left to reason about
    assert len(row.logs) == 2
    assert row.worker == "echo_abc_1"
    assert delta.stopping == ["echo_abc_1"]


def test_an_extend_says_what_the_epochs_became(tmp_path: Path) -> None:
    """`epochs 1 → 3` is what a reader can act on; a sample count only says it grew."""
    delta = compute_delta(
        synth_manifest([ECHO_3]),
        synth_manifest([ECHO]),
        logs=empty(),
        archived=empty(),
        running=[],
    )

    (row,) = delta.of(Change.EXTEND)
    assert row.epochs == (1, 3)
    assert row.samples == ECHO_3.samples * 3


def test_the_rows_are_ordered_additive_first(tmp_path: Path) -> None:
    """What the reader meets first is what they asked for; what it costs comes after."""
    delta = compute_delta(
        synth_manifest([ADDITION_SCALED, ECHO_3]),
        synth_manifest([ADDITION, ECHO]),
        logs=empty(),
        archived=empty(),
        running=[],
    )

    assert [row.change for row in delta.changes] == [
        Change.ADD,
        Change.EXTEND,
        Change.SUPERSEDED,
    ]


def test_a_moved_log_directory_is_not_additive_even_with_no_rows(
    tmp_path: Path,
) -> None:
    """The hole a row-counting gate leaves.

    Every identifier survives a `log_dir` edit, so the table is empty and the
    delta looks like the safest thing there is — while committing it re-runs the
    whole sweep into a new directory and leaves the results in the old one.
    """
    old_logs = tmp_path / "logs"
    for task in (ADDITION, ECHO):
        write_log(old_logs, task)

    delta = compute_delta(
        synth_manifest([ADDITION, ECHO]),
        synth_manifest([ADDITION, ECHO]),
        logs=ObservedLogs(log_dir=str(tmp_path / "logs2")),
        archived=empty(),
        running=[],
        stranded=observe_logs(str(old_logs)),
    )

    assert delta.changes == []
    assert delta.empty is False
    assert delta.additive is False
    assert delta.relocated is not None
    assert (delta.relocated.old, delta.relocated.new) == (
        str(old_logs),
        str(tmp_path / "logs2"),
    )
    assert delta.relocated.stranded == 2


def test_results_already_copied_into_the_new_directory_are_not_stranded(
    tmp_path: Path,
) -> None:
    """Counting the old directory wholesale would refuse a move that costs nothing.

    Copying the logs across before relaunching is the sensible way to move a run,
    and a task whose results are already in the new directory will not run again.
    The move is still reported — it is a real change and the reader should see it
    — but with nothing attributed to it.
    """
    old_logs = tmp_path / "logs"
    new_logs = tmp_path / "logs2"
    for task in (ADDITION, ECHO):
        write_log(old_logs, task)
        write_log(new_logs, task)

    delta = compute_delta(
        synth_manifest([ADDITION, ECHO]),
        synth_manifest([ADDITION, ECHO]),
        logs=observe_logs(str(new_logs)),
        archived=empty(),
        running=[],
        stranded=observe_logs(str(old_logs)),
    )

    assert delta.relocated is not None and delta.relocated.stranded == 0
    # still not additive: the reader is being told the run's directory changed,
    # and a launch that moved it silently is the thing being prevented
    assert delta.additive is False


def test_an_unmoved_log_directory_reports_no_relocation(tmp_path: Path) -> None:
    """The ordinary case, which must not pay for the rare one.

    `stranded` is `None` when the two directories agree, so nothing extra is read
    and nothing extra is reported.
    """
    delta = compute_delta(
        synth_manifest([ADDITION]),
        synth_manifest([ADDITION]),
        logs=empty(),
        archived=empty(),
        running=[],
    )

    assert delta.relocated is None
    assert (delta.additive, delta.empty) == (True, True)


def test_a_relocation_stops_every_live_worker(tmp_path: Path) -> None:
    """A worker's log directory is decided when it spawns, and it cannot be told otherwise.

    Its selection document names the old directory, so after a relocation every
    worker in flight is writing hours of work into a place the run no longer
    reads. Nothing archives it and no later turn finds it: the task simply runs
    again once the worker exits. So the fleet is stopped, and stopping it is
    part of what the operator is accepting.

    **Every worker, not only the ones whose tasks changed.** Any worker alive
    when the manifest is committed was spawned against the manifest being
    replaced, so all of them are writing to the old directory whatever their
    task's row says — including one running a task no manifest names.
    """
    old_logs = tmp_path / "logs"
    write_log(old_logs, ADDITION)

    delta = compute_delta(
        synth_manifest([ADDITION, ECHO]),
        synth_manifest([ADDITION, ECHO]),
        logs=ObservedLogs(log_dir=str(tmp_path / "logs2")),
        archived=empty(),
        running=[
            running(ADDITION, "addition_aaa_1"),
            running(ECHO, "echo_bbb_1"),
        ],
        stranded=observe_logs(str(old_logs)),
    )

    assert delta.relocated is not None
    assert delta.relocated.workers == ("addition_aaa_1", "echo_bbb_1")
    assert delta.stopping == ["addition_aaa_1", "echo_bbb_1"]


def test_a_packed_worker_loses_only_the_task_that_left(tmp_path: Path) -> None:
    """Stopping the process would cost the tasks nobody asked to stop.

    At the default width a task leaving the manifest and its process ending are
    the same act. Packed they come apart, and the delta has to say which of the
    two a caller is being told about — `leaving` is the subset, `wholesale` the
    processes that go regardless.
    """
    logs = tmp_path / "logs"
    write_log(logs, ECHO)
    packed = RunningWorker(
        worker="batch_abc_1",
        identifiers=(ADDITION.identifier, ECHO.identifier),
        pid=1,
        host="here",
    )

    delta = compute_delta(
        synth_manifest([ADDITION]),
        synth_manifest([ADDITION, ECHO]),
        logs=observe_logs(str(logs)),
        archived=empty(),
        running=[packed],
    )

    # the process is named, because something about it has to change
    assert delta.stopping == ["batch_abc_1"]
    # but only the departed task is going, so the survivor keeps running
    assert delta.leaving == {ECHO.identifier}
    assert delta.wholesale == set()


def test_a_relocation_takes_a_packed_worker_whole(tmp_path: Path) -> None:
    # the directory moved out from under everything in the process, so there is
    # no subset to compute and nothing in it is worth keeping where it is
    packed = RunningWorker(
        worker="batch_abc_1",
        identifiers=(ADDITION.identifier, ECHO.identifier),
        pid=1,
        host="here",
    )

    delta = compute_delta(
        synth_manifest([ADDITION, ECHO]),
        synth_manifest([ADDITION, ECHO]),
        logs=ObservedLogs(log_dir=str(tmp_path / "logs2")),
        archived=empty(),
        running=[packed],
        stranded=ObservedLogs(log_dir=str(tmp_path / "logs")),
    )

    assert delta.wholesale == {"batch_abc_1"}
    assert delta.leaving == set()


def test_a_worker_named_by_both_a_relocation_and_an_archive_row_is_stopped_once(
    tmp_path: Path,
) -> None:
    """The two reasons overlap, and stopping a worker twice would signal a pid that is already gone."""
    delta = compute_delta(
        synth_manifest([ADDITION]),
        synth_manifest([ADDITION, ECHO]),
        logs=ObservedLogs(log_dir=str(tmp_path / "logs2")),
        archived=empty(),
        running=[running(ECHO, "echo_bbb_1")],
        stranded=ObservedLogs(log_dir=str(tmp_path / "logs")),
    )

    assert [row.worker for row in delta.of(Change.REMOVED)] == ["echo_bbb_1"]
    assert delta.relocated is not None
    assert delta.stopping == ["echo_bbb_1"]
