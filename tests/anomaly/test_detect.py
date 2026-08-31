"""The task-level signatures and the composition of a turn's census.

Sample-level classification is `test_instances.py`'s subject; here the claims are the ones only composition can make: an errored log classes on its header, a started log with no worker is `vanished`, a departure with no log classes on its tail, a zero headline needs its confirming read, and an orphan's failures are nobody's anomaly.
"""

from dataclasses import replace
from pathlib import Path

from inspect_steward._anomaly.fold import TaskHealth
from inspect_steward._evalset.instances import ClassedCache
from inspect_steward._evalset.observe import UnreadableLog, observe_logs, observe_tasks
from inspect_steward._schedule.reconcile import (
    DepartedWorker,
    InFlight,
    RunningWorker,
)
from inspect_steward._tend.detect import (
    UNIFORM_ZERO_MIN,
    detect,
    task_health,
)
from inspect_steward._worker.live import LiveFleet

from .._logs import SynthSample, SynthTask, synth_manifest, write_log

SCORER_TRACEBACK = """Traceback (most recent call last):
  File "/work/evals/scorer.py", line 15, in score
    raise ScorerError("no grade")
evals.scorer.ScorerError: no grade
"""

IMPORT_TAIL = """collecting tasks...
Traceback (most recent call last):
  File "/work/evalset.py", line 3, in <module>
    from missing_lib import thing
ModuleNotFoundError: No module named 'missing_lib'
"""


def census(
    log_dir: Path,
    tasks: list[SynthTask],
    *,
    inflight: InFlight | None = None,
    workers_dir: Path | None = None,
) -> "list[str]":
    detection = run(log_dir, tasks, inflight=inflight, workers_dir=workers_dir)
    return [batch.class_key for batch in detection.batches]


def run(
    log_dir: Path,
    tasks: list[SynthTask],
    *,
    inflight: InFlight | None = None,
    workers_dir: Path | None = None,
):  # noqa: ANN201 -- the Detection type is the assertion surface
    logs = observe_logs(log_dir)
    return detect(
        observe_tasks(synth_manifest(tasks), logs),
        logs,
        inflight or InFlight(),
        LiveFleet(),
        workers_dir=workers_dir or (log_dir / "workers"),
        cache=ClassedCache(),
    )


class TestTaskError:
    def test_an_errored_log_classes_on_its_header(self, tmp_path: Path) -> None:
        task = SynthTask("probe")
        write_log(
            tmp_path,
            task,
            error="ScorerError('no grade')",
            error_traceback=SCORER_TRACEBACK,
        )

        detection = run(tmp_path, [task])

        assert [b.class_key for b in detection.batches] == [
            "task:error:evals.scorer.ScorerError@evals/scorer.py:score"
        ]
        instance = detection.batches[0].instances[0]
        assert instance.task == task.identifier
        assert instance.ref.startswith(f"{task.identifier}@")

    def test_a_cancelled_error_is_teardown(self, tmp_path: Path) -> None:
        task = SynthTask("probe")
        write_log(tmp_path, task, error="CancelledError()")

        assert census(tmp_path, [task]) == []

    def test_an_orphans_failures_are_nobodys_anomaly(self, tmp_path: Path) -> None:
        stranger = SynthTask("stranger")
        write_log(tmp_path, stranger, error="ValueError('x')")

        # the manifest names a different task entirely
        missing = SynthTask("wanted")
        assert census(tmp_path, [missing]) == []


class TestVanished:
    def test_a_started_log_with_no_worker_is_vanished(self, tmp_path: Path) -> None:
        task = SynthTask("probe")
        write_log(tmp_path, task, status="started")

        assert census(tmp_path, [task]) == ["task:vanished"]

    def test_a_started_log_whose_worker_lives_is_just_running(
        self, tmp_path: Path
    ) -> None:
        task = SynthTask("probe")
        write_log(tmp_path, task, status="started")
        inflight = InFlight(
            running=[
                RunningWorker(
                    worker="w1", identifiers=(task.identifier,), pid=4242, host="here"
                )
            ]
        )

        assert census(tmp_path, [task], inflight=inflight) == []


class TestNoLog:
    def test_a_departure_with_no_log_classes_on_its_tail(self, tmp_path: Path) -> None:
        task = SynthTask("probe")
        workers = tmp_path / "workers"
        workers.mkdir()
        (workers / "w1.log").write_text(IMPORT_TAIL, encoding="utf-8")
        inflight = InFlight(
            departed=[
                DepartedWorker(
                    worker="w1",
                    identifiers=(task.identifier,),
                    host="here",
                    pid=4242,
                    started="2026-08-30T10:00:00Z",
                )
            ]
        )

        detection = run(tmp_path, [task], inflight=inflight, workers_dir=workers)

        assert [b.class_key for b in detection.batches] == [
            "task:no-log-exit:ModuleNotFoundError@work/evalset.py:<module>"
        ]
        instance = detection.batches[0].instances[0]
        assert instance.ref == f"{task.identifier}@w1"
        assert instance.attempt_created == "2026-08-30T10:00:00Z"
        assert "missing_lib" in instance.message

    def test_a_substrate_tail_flags_the_class(self, tmp_path: Path) -> None:
        # a worker dying on the machinery under the run must carry the flag,
        # or the no-re-run-proposal guard never sees the case it exists for
        tail = (
            "Traceback (most recent call last):\n"
            '  File "/venv/aiobotocore/credentials.py", line 12, in load\n'
            "    raise NoCredentialsError()\n"
            "botocore.exceptions.NoCredentialsError: Unable to locate credentials\n"
        )
        task = SynthTask("probe")
        workers = tmp_path / "workers"
        workers.mkdir()
        (workers / "w1.log").write_text(tail, encoding="utf-8")
        inflight = InFlight(
            departed=[
                DepartedWorker(
                    worker="w1",
                    identifiers=(task.identifier,),
                    host="here",
                    started="2026-08-30T10:00:00Z",
                )
            ]
        )

        detection = run(tmp_path, [task], inflight=inflight, workers_dir=workers)

        assert detection.batches[0].substrate is True

    def test_a_departure_that_wrote_nothing_is_the_bare_bucket(
        self, tmp_path: Path
    ) -> None:
        task = SynthTask("probe")
        inflight = InFlight(
            departed=[
                DepartedWorker(
                    worker="w1",
                    identifiers=(task.identifier,),
                    host="here",
                    started="2026-08-30T10:00:00Z",
                )
            ]
        )

        detection = run(tmp_path, [task], inflight=inflight)

        assert [b.class_key for b in detection.batches] == ["task:no-log"]
        assert detection.batches[0].instances[0].message == "no output at all"

    def test_a_departure_that_landed_a_log_is_not_no_log(self, tmp_path: Path) -> None:
        # the log it landed reads as the errored/vanished case instead; here it
        # errored, so the census says that and only that
        task = SynthTask("probe")
        write_log(
            tmp_path, task, error="ValueError('x')", created="2026-08-30T11:00:00+00:00"
        )
        inflight = InFlight(
            departed=[
                DepartedWorker(
                    worker="w1",
                    identifiers=(task.identifier,),
                    host="here",
                    started="2026-08-30T10:00:00Z",
                )
            ]
        )

        assert census(tmp_path, [task], inflight=inflight) == [
            "task:error:ValueError@unknown"
        ]


class TestUniformZero:
    def zeroed(self, count: int) -> list[SynthSample]:
        return [SynthSample(id=f"s{n}", score=0.0) for n in range(count)]

    def test_a_zero_headline_with_confirming_scores_is_detected(
        self, tmp_path: Path
    ) -> None:
        task = SynthTask("probe", samples=UNIFORM_ZERO_MIN)
        write_log(
            tmp_path,
            task,
            scores={"exact": {"accuracy": 0.0}},
            samples=self.zeroed(UNIFORM_ZERO_MIN),
        )

        keys = census(tmp_path, [task])

        assert len(keys) == 1
        # named by the display key, sanitized, plus the identifier's digest
        assert keys[0].startswith("score:zero:probe")

    def test_a_nonzero_headline_confirms_nothing(self, tmp_path: Path) -> None:
        task = SynthTask("probe", samples=UNIFORM_ZERO_MIN)
        write_log(
            tmp_path,
            task,
            scores={"exact": {"accuracy": 0.4}},
            samples=self.zeroed(UNIFORM_ZERO_MIN),
        )

        assert census(tmp_path, [task]) == []

    def test_a_small_task_never_trips_it(self, tmp_path: Path) -> None:
        small = SynthTask("probe", samples=UNIFORM_ZERO_MIN - 1)
        write_log(
            tmp_path,
            small,
            scores={"exact": {"accuracy": 0.0}},
            samples=self.zeroed(UNIFORM_ZERO_MIN - 1),
        )

        assert census(tmp_path, [small]) == []

    def test_a_zero_headline_over_mixed_scores_confirms_nothing(
        self, tmp_path: Path
    ) -> None:
        # the cancellation-over-±1 shape: the headline nets to zero and the
        # samples do not agree
        task = SynthTask("probe", samples=UNIFORM_ZERO_MIN)
        mixed = [
            SynthSample(id=f"s{n}", score=1.0 if n % 2 else -1.0)
            for n in range(UNIFORM_ZERO_MIN)
        ]
        write_log(tmp_path, task, scores={"exact": {"accuracy": 0.0}}, samples=mixed)

        assert census(tmp_path, [task]) == []


class TestComposition:
    def test_one_failure_across_tasks_is_one_batch(self, tmp_path: Path) -> None:
        first, second = SynthTask("alpha"), SynthTask("beta")
        for task in (first, second):
            write_log(
                tmp_path,
                task,
                completed=task.samples - 1,
                samples=[
                    SynthSample(
                        id="s1",
                        error="ScorerError('no grade')",
                        traceback=SCORER_TRACEBACK,
                    )
                ],
            )

        detection = run(tmp_path, [first, second])

        assert [b.class_key for b in detection.batches] == [
            "error:evals.scorer.ScorerError@evals/scorer.py:score"
        ]
        batch = detection.batches[0]
        assert len(batch.instances) == 2
        assert {i.task for i in batch.instances} == {
            first.identifier,
            second.identifier,
        }

    def test_task_health_reads_completion_off_the_observation(
        self, tmp_path: Path
    ) -> None:
        done, waiting = SynthTask("done"), SynthTask("waiting")
        write_log(tmp_path, done, created="2026-08-30T11:00:00+00:00")
        logs = observe_logs(tmp_path)

        health = task_health(observe_tasks(synth_manifest([done, waiting]), logs))

        assert health[done.identifier] == TaskHealth(
            complete=True, settled="2026-08-30T11:00:00+00:00"
        )
        assert health[waiting.identifier] == TaskHealth(complete=False)

    def test_an_unreadable_log_holds_its_task_out_of_recovered(
        self, tmp_path: Path
    ) -> None:
        # both pass branches trust the census's silence, and an unreadable log
        # makes that silence blindness -- health is the one place they already
        # look, so a task whose log will not read is not recovered, whatever
        # its headers say
        done = SynthTask("done")
        write_log(tmp_path, done)
        observed = observe_tasks(synth_manifest([done]), observe_logs(tmp_path))
        current = observed.tasks[0].current
        assert current is not None
        blinded = replace(
            observed,
            unreadable=[
                UnreadableLog(location=current.location, reason="summaries truncated")
            ],
        )

        health = task_health(blinded)

        assert health[done.identifier] == TaskHealth(complete=False)
