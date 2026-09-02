"""Rung 2 of the convergence ladder: a task another project already ran.

`logs-archive/` is free and local, the store is cheap and global, and a worker is neither — so a launch consults them in that order and spawns what neither answered (execution.md §5.6).

**What makes the read work here and not in Flow is one predicate.** `reconcile._spawn_order` queues only `MISSING` and `INCOMPLETE` tasks, so a log that lands in `logs/` leaves its task `COMPLETE` and nothing is ever started for it. Flow's own read half copies a log and then runs the task anyway — selection mode deliberately skips `eval_set()`'s reuse logic — so the copy bought nothing and cost a race between N workers writing one destination. The read belongs to the single writer, and this file is the assertion that it pays off there.
"""

import importlib
from pathlib import Path

import pytest
from inspect_steward._evalset.observe import ObservedLogs, observe_logs
from inspect_steward._launch import Launch, launch
from inspect_steward._launch.delta import compute_delta
from inspect_steward._schedule import InFlight, RunningWorker
from inspect_steward._store import StoreError
from inspect_steward._tend import TendResult
from inspect_steward._workspace import Workspace, create_workspace, read_journal

from .._logs import SynthTask, synth_manifest, write_log
from ..timer._fake import clear_credentials, fake_cron
from ._fake import fake_capture

ADDITION = SynthTask("addition", samples=2)


def stored(tmp_path: Path, *tasks: SynthTask) -> Path:
    """A directory store holding a complete log for each task."""
    location = tmp_path / "store"
    for task in tasks:
        write_log(location, task)
    return location


def launched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *tasks: SynthTask,
    store: Path | None = None,
) -> Launch:
    """Capture `tasks` and launch against `store`, without a timer or a subprocess."""
    create_workspace(tmp_path, git=False)
    fake_cron(monkeypatch)
    clear_credentials(monkeypatch)
    fake_capture(monkeypatch, synth_manifest(list(tasks)))
    result = launch(
        Workspace.at(tmp_path),
        tmp_path / "evalset.py",
        timer=False,
        log_store=str(store) if store is not None else None,
    )
    assert isinstance(result, Launch)
    return result


class TestATaskTheStoreAlreadyHas:
    def test_its_log_is_copied_into_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = launched(
            tmp_path, monkeypatch, ADDITION, store=stored(tmp_path, ADDITION)
        )

        assert [one.identifier for one in result.reused] == [ADDITION.identifier]
        assert list((tmp_path / "logs").glob("*.json"))

    def test_and_nothing_is_started_for_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # **the whole point, and the property Flow's read half lacks.**
        # `_spawn_order` queues only `MISSING` and `INCOMPLETE`, so the copied
        # log settles the task rather than sitting beside a fresh one
        result = launched(
            tmp_path, monkeypatch, ADDITION, store=stored(tmp_path, ADDITION)
        )

        assert isinstance(result.turn, TendResult)
        assert result.turn.queued == []
        assert result.turn.summary.states.get("complete") == 1

    def test_the_source_is_journaled_and_not_only_the_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # an identifier match says the task, args, model, solver, resolved plan
        # and execution limits were identical and says nothing about the
        # environment it ran in -- so whose log this is has to stay answerable
        store = stored(tmp_path, ADDITION)

        launched(tmp_path, monkeypatch, ADDITION, store=store)

        events = read_journal(Workspace.at(tmp_path).journal).events
        launch_event = next(one for one in events if one.type == "launched")
        reused = launch_event.payload["reused"]
        assert len(reused) == 1
        assert "store" in reused[0]["source"]

    def test_a_task_the_store_does_not_have_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = launched(tmp_path, monkeypatch, ADDITION, store=tmp_path / "empty")

        assert result.reused == []


class TestALogThatMatchesAndDoesNotAnswer:
    """A task identifier is not a promise about the results, and the store searches on nothing else.

    `task_identifier` hashes the solver plan, generate config, model args, roles, version and execution limits — and pointedly *not* the sample count, the epochs or the selection, so that raising any of them leaves existing logs resumable rather than orphaning them. So a store is free to hand back a log for the same identifier that ran a different slice or fewer samples. Copying one in leaves the task `INCOMPLETE`, the next tend queuing it, and the launch having already told the operator and the journal that the work does not run here.
    """

    def test_a_log_short_of_what_the_run_asks_is_not_claimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the dataset grew, or last month's run was smaller: same identifier,
        # same selection, and half the samples this run wants
        store = tmp_path / "store"
        write_log(store, ADDITION, total=1, completed=1)

        result = launched(tmp_path, monkeypatch, ADDITION, store=store)

        assert result.reused == []

    def test_and_is_not_copied_in_either(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # not claimed *and* not landed: a log answering a different question
        # would be superseded on arrival, and signoff would then curate a
        # foreign attempt this project never ran
        store = tmp_path / "store"
        write_log(store, ADDITION, total=1, completed=1)

        launched(tmp_path, monkeypatch, ADDITION, store=store)

        assert not list((tmp_path / "logs").glob("*.json"))

    def test_a_log_that_ran_a_different_slice_is_not_claimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `answers_shape`, the same predicate the archive restore already asks
        # before calling a restored log a reason the work does not run again
        store = tmp_path / "store"
        write_log(store, ADDITION, selection={"sample_shuffle": 42})

        result = launched(tmp_path, monkeypatch, ADDITION, store=store)

        assert result.reused == []

    def test_a_signed_log_carrying_accepted_errors_is_reusable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # **eligibility and reconciliation have to be one predicate.** A run
        # signed with samples somebody accepted as errored has a full
        # `total_samples` and a short `completed_samples` — which observation
        # calls complete and a hand-rolled check on completed samples refused.
        # So signoff published the log and the next launch would not take it
        store = tmp_path / "store"
        write_log(store, ADDITION, total=2, completed=1)

        result = launched(tmp_path, monkeypatch, ADDITION, store=store)

        assert [one.identifier for one in result.reused] == [ADDITION.identifier]

    def test_a_full_count_errored_log_is_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # and the mirror of it: full samples, `status="error"`, which
        # reconciliation queues and the old check called reused
        store = tmp_path / "store"
        write_log(store, ADDITION, status="error", total=2, completed=2)

        result = launched(tmp_path, monkeypatch, ADDITION, store=store)

        assert result.reused == []

    def test_a_second_candidate_is_tried_when_the_best_ranked_one_misses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # **the store ranks by size and recency and cannot see the question.**
        # The bigger log ran a different slice; the smaller one answers exactly.
        # Checking only the front of the list made the filter a veto and left
        # the store's real answer unfound
        store = tmp_path / "store"
        write_log(store, ADDITION, total=4, completed=4, selection={"limit": 4})
        answers = write_log(store, ADDITION, total=2, completed=2)

        result = launched(tmp_path, monkeypatch, ADDITION, store=store)

        assert [one.identifier for one in result.reused] == [ADDITION.identifier]
        assert result.reused[0].source.endswith(answers.name)

    def test_a_log_that_will_not_read_costs_one_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = tmp_path / "store"
        write_log(store, ADDITION)
        for one in store.glob("*.json"):
            one.write_text("torn", encoding="utf-8")

        result = launched(tmp_path, monkeypatch, ADDITION, store=store)

        assert result.committed
        assert result.reused == []


class TestWhenThereIsNoStoreToRead:
    def test_a_workspace_with_none_configured_reuses_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = launched(tmp_path, monkeypatch, ADDITION)

        assert result.reused == []

    def test_a_store_that_will_not_open_warns_and_launches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # **a store is an optimisation whose absence costs time and never
        # correctness**, so it cannot be allowed to cost a launch either: one
        # recorded line, and the task runs the ordinary way
        # `importlib` rather than `import ... as module`, which binds the
        # *function* `_launch/__init__.py` re-exports under the same name
        module = importlib.import_module("inspect_steward._launch.launch")

        def refuse(location: str, *, root: Path) -> object:
            raise StoreError("the bucket is not there")

        monkeypatch.setattr(module, "open_store", refuse)

        result = launched(
            tmp_path, monkeypatch, ADDITION, store=stored(tmp_path, ADDITION)
        )

        assert result.committed
        assert result.reused == []
        assert any("nothing could be reused" in one for one in result.failures)


class Provider(Exception):
    """A backend's own exception, standing in for `botocore.exceptions.NoCredentialsError`.

    Deliberately not an `OSError` or a `ValueError`, which is the whole property under test: fsspec backends raise their own hierarchies and none of them are Python's.
    """


class TestFailuresFromSomebodyElsesHierarchy:
    """`StoreError` covers the query and nothing after it, and what comes after it also touches the store.

    `_store` normalizes its own failures, so the search is safe. Reading a candidate's header and copying it in are then two more remote operations on the same backend, and they were guarded against `OSError` and `ValueError` — so an S3 store whose credentials expired between the query and the copy raised out of a launch that had **already committed its manifest**. Rung 2 is an optimisation whose every failure is one task that runs the ordinary way, and that has to include the failures nothing here anticipated.
    """

    def test_a_candidate_that_will_not_read_leaves_the_task_to_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = importlib.import_module("inspect_steward._launch.launch")

        def refuse(source: str) -> object:
            raise Provider("the credentials expired")

        monkeypatch.setattr(module, "read_attempt", refuse)

        result = launched(
            tmp_path, monkeypatch, ADDITION, store=stored(tmp_path, ADDITION)
        )

        assert result.committed
        assert result.reused == []

    def test_a_copy_that_the_backend_refuses_is_a_recorded_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = importlib.import_module("inspect_steward._launch.launch")

        def refuse(source: str, log_dir: str) -> str:
            raise Provider("the credentials expired")

        monkeypatch.setattr(module, "_copy_in", refuse)

        result = launched(
            tmp_path, monkeypatch, ADDITION, store=stored(tmp_path, ADDITION)
        )

        assert result.committed
        assert result.reused == []
        assert any("could not copy" in one for one in result.failures)


def busy(monkeypatch: pytest.MonkeyPatch, *tasks: SynthTask) -> None:
    """Put a live worker on each task, in the window before it has landed a log."""
    module = importlib.import_module("inspect_steward._launch.launch")
    fleet = InFlight(
        running=[
            # `socket=None` is the pre-boundary window itself: spawned, no log,
            # no control entry — and the record still knows what it is running
            RunningWorker(
                worker=f"w-{index}",
                identifiers=(task.identifier,),
                pid=1,
                host="here",
            )
            for index, task in enumerate(tasks)
        ]
    )

    def resolved(record: Path, workers: Path) -> InFlight:
        return fleet

    monkeypatch.setattr(module, "resolve_inflight", resolved)


class TestATaskSomebodyIsAlreadyRunning:
    """A worker's log exists from the moment it starts — except across the pre-boundary window.

    A process that has been spawned and has not reached its `eval_set()` boundary has no log and no control socket, so its identifier looked exactly like one nothing had ever run. The store answered it, the launch reported the work as satisfied and not running here, and the worker went on spending money on the task for as long as it took. `reconcile` already declines to queue a running task on the strength of the same in-flight record — this is that rule one rung up the ladder rather than a second one.
    """

    def test_a_running_identifier_is_not_satisfied_from_the_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        busy(monkeypatch, ADDITION)

        result = launched(
            tmp_path, monkeypatch, ADDITION, store=stored(tmp_path, ADDITION)
        )

        assert result.reused == []

    def test_and_no_log_is_copied_in_underneath_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the sharper half: a copy lands in the directory the worker is writing
        # to, so the task reads complete while the process that owns it is still
        # going -- two attempts, one of them nobody asked for
        busy(monkeypatch, ADDITION)

        launched(tmp_path, monkeypatch, ADDITION, store=stored(tmp_path, ADDITION))

        assert not list((tmp_path / "logs").glob("*.json"))

    def test_a_worker_being_stopped_outright_leaves_its_task_reusable(
        self, tmp_path: Path
    ) -> None:
        # relocation and reshaping take the process down whatever it holds, so
        # its tasks begin again from nothing -- which is exactly where a store
        # result is the difference between a re-run and no run at all
        module = importlib.import_module("inspect_steward._launch.launch")
        old_logs = tmp_path / "logs"
        write_log(old_logs, ADDITION)
        worker = RunningWorker(
            worker="addition_aaa_1",
            identifiers=(ADDITION.identifier,),
            pid=1,
            host="here",
        )
        delta = compute_delta(
            synth_manifest([ADDITION]),
            synth_manifest([ADDITION]),
            logs=ObservedLogs(log_dir=str(tmp_path / "logs2")),
            archived=ObservedLogs(log_dir="nowhere"),
            running=[worker],
            stranded=observe_logs(str(old_logs)),
        )

        assert delta.relocated is not None
        assert module._held(delta, [worker]) == set()

    def test_where_a_worker_staying_up_holds_its_tasks(self, tmp_path: Path) -> None:
        module = importlib.import_module("inspect_steward._launch.launch")
        worker = RunningWorker(
            worker="addition_aaa_1",
            identifiers=(ADDITION.identifier,),
            pid=1,
            host="here",
        )
        delta = compute_delta(
            synth_manifest([ADDITION]),
            synth_manifest([ADDITION]),
            logs=ObservedLogs(log_dir=str(tmp_path / "logs")),
            archived=ObservedLogs(log_dir="nowhere"),
            running=[worker],
        )

        assert module._held(delta, [worker]) == {ADDITION.identifier}


class TestACopyThatDidNotFinish:
    """A name is only evidence of a log where nothing was interrupted putting it there.

    Skipping on the name alone was right about the ordinary case and wrong about the only case that reaches the reuse copy at all: the wanted set is built from `observe_logs`, which *skips a log it cannot read*, so an identifier arrives wanted precisely when the file under that name is one nothing could parse.
    """

    def test_wreckage_under_the_final_name_is_replaced_rather_than_believed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = stored(tmp_path, ADDITION)
        source = next(store.iterdir())
        landing = tmp_path / "logs"
        landing.mkdir()
        # what an interrupted copy leaves: the final path, the final name, and
        # a file no reader can open
        whole = source.read_bytes()
        (landing / source.name).write_bytes(whole[: len(whole) // 3])

        result = launched(tmp_path, monkeypatch, ADDITION, store=store)

        assert [one.identifier for one in result.reused] == [ADDITION.identifier]
        assert (landing / source.name).read_bytes() == whole
