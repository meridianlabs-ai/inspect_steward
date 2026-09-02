"""What the samples did, which is not what the tasks did.

**A green exit is the normal shape of the failure a smoke exists to find.** Workers force `continue_on_fail`, so a wrong key, a sandbox image that will not start, and a scorer that throws all land as errored samples inside a log whose status is `success` and whose task finished — and a rehearsal that asked only *did the tasks finish* answered yes to every one of them. Measured: four samples landed, two errored, and the rehearsal passed with nothing to say about it.
"""

from pathlib import Path

from inspect_steward._evalset.observe import ObservedLogs, observe_logs
from inspect_steward._smoke.checks import Probe
from inspect_steward._smoke.digest import Outcome, Smoke, digest_markdown, outcome
from inspect_steward._smoke.run import failures, unfinished

from .._logs import SynthSample, SynthTask, synth_manifest, write_log

TIMEOUT = "ReadTimeout('the provider went away')"
TRACEBACK = (
    'Traceback (most recent call last):\n  File "/x/openai/_client.py", line 9, '
    "in post\n    raise ReadTimeout\nReadTimeout: the provider went away"
)
CLASS = "error:ReadTimeout@openai/_client.py:post"

ADDITION = SynthTask("addition", samples=4)
TASK = synth_manifest([ADDITION])


def errored(id: str) -> SynthSample:
    return SynthSample(id, error=TIMEOUT, traceback=TRACEBACK)


def landed(tmp_path: Path, *samples: SynthSample) -> tuple[tuple[str, ...], int]:
    """Write one settled log holding these samples, and class what it holds."""
    write_log(tmp_path, ADDITION, samples=list(samples))
    return failures(observe_logs(str(tmp_path)))


class TestWhatTheSamplesDid:
    def test_a_settled_log_of_errored_samples_is_not_a_clean_rehearsal(
        self, tmp_path: Path
    ) -> None:
        # the whole file: the log's status is `success`, the task finished, and
        # half the samples never ran
        lines, count = landed(
            tmp_path,
            SynthSample("1"),
            errored("2"),
            errored("3"),
            SynthSample("4"),
        )

        assert count == 2
        assert lines == (f"2 samples errored the same way — {CLASS}",)

    def test_samples_that_failed_the_same_way_are_one_line(
        self, tmp_path: Path
    ) -> None:
        # in the tend's own words, through `class_summary`, so a finding is not
        # described one way before the launch and another way during it
        lines, count = landed(tmp_path, *(errored(str(one)) for one in range(4)))

        assert count == 4
        assert lines == (f"4 samples errored the same way — {CLASS}",)

    def test_an_operator_limit_is_reported_and_does_not_block(
        self, tmp_path: Path
    ) -> None:
        # a sample stopped by an operator limit ran as designed. Reporting it is
        # useful; failing the rehearsal over it would refuse a legitimate run
        lines, count = landed(
            tmp_path,
            SynthSample("1", limit="operator", limit_reason="stopped"),
            SynthSample("2", limit="operator", limit_reason="stopped"),
            SynthSample("3"),
        )

        assert count == 0
        assert lines == ("2 samples were terminated by an operator",)

    def test_a_clean_log_says_nothing(self, tmp_path: Path) -> None:
        assert landed(tmp_path, SynthSample("1"), SynthSample("2")) == ((), 0)


class TestWhatTheTasksDid:
    """Settled and *finished well* are different questions.

    `settled` asks whether the watch can stop, so a log finalized `error` satisfies it — a task that died is not one to keep waiting for. But a task-level failure lands no errored samples to class, so the sample census sees a clean run of zero and the rehearsal reported ready. A definition that will not import is the plainest case there is of what a smoke exists to catch.
    """

    def observed(self, tmp_path: Path) -> ObservedLogs:
        return observe_logs(str(tmp_path))

    def test_a_task_that_died_is_not_a_clean_rehearsal(self, tmp_path: Path) -> None:
        write_log(tmp_path, ADDITION, error="ImportError('no module named x')")

        lines = unfinished(TASK, self.observed(tmp_path))

        assert len(lines) == 1
        assert "error" in lines[0]
        assert "task:error:ImportError" in lines[0]

    def test_a_task_with_no_log_at_all_is_named(self, tmp_path: Path) -> None:
        assert unfinished(TASK, self.observed(tmp_path)) == (
            [f"{TASK.tasks[0].key} produced no log"]
        )

    def test_a_task_still_running_is_named_as_that(self, tmp_path: Path) -> None:
        write_log(tmp_path, ADDITION, status="started")

        assert unfinished(TASK, self.observed(tmp_path)) == (
            [f"{TASK.tasks[0].key} did not finish"]
        )

    def test_a_task_that_finished_says_nothing(self, tmp_path: Path) -> None:
        write_log(tmp_path, ADDITION, samples=[SynthSample("1")])

        assert unfinished(TASK, self.observed(tmp_path)) == []


class TestWhatTheScannersDid:
    """A scanner that threw is the scan path saying it does not work.

    Those arrive as `scanerror:` classes among the findings, which are reported and count toward nothing — so every scanner could fail on every transcript while the verdict read *rehearsed and ready* and the journal recorded a pass. During a run the same class is a question for a person, since the samples are fine and only the reading of them failed; before one it is what a rehearsal is for.
    """

    def test_a_scanner_that_threw_fails_the_rehearsal(self) -> None:
        assert (
            outcome(Probe(), waived=(), capped=False, errors=0, errored=0, threw=3)
            is Outcome.FAILED
        )

    def test_the_verdict_says_so(self) -> None:
        smoke = Smoke(outcome=Outcome.FAILED, threw=3)

        assert digest_markdown(smoke).splitlines()[2] == (
            "**🛑 not ready to launch — a scanner threw on 3 transcripts**"
        )

    def test_it_is_named_beside_whatever_else_failed(self) -> None:
        # every reason at once, on the rule signoff already keeps: fixing one and
        # being refused for the next is a document that knew both
        smoke = Smoke(outcome=Outcome.FAILED, errored=1, landed=4, threw=2)

        assert digest_markdown(smoke).splitlines()[2] == (
            "**🛑 not ready to launch — 1 of 4 samples errored; a scanner threw "
            "on 2 transcripts**"
        )


class TestWhatItDoesToTheVerdict:
    def test_an_errored_sample_fails_the_rehearsal(self) -> None:
        assert (
            outcome(Probe(), waived=(), capped=False, errors=0, errored=1)
            is Outcome.FAILED
        )

    def test_and_is_not_waivable(self) -> None:
        # `--accept` waives a *check* -- a question about configuration a person
        # can answer better than Steward can. A sample that errored is not a
        # question, it is the answer arriving
        every = ("context_window", "reasoning", "reasoning_api")

        assert (
            outcome(Probe(), waived=every, capped=False, errors=0, errored=1)
            is Outcome.FAILED
        )

    def test_the_verdict_names_it_beside_whatever_else_failed(self) -> None:
        smoke = Smoke(outcome=Outcome.FAILED, errored=2, landed=4)

        assert digest_markdown(smoke).splitlines()[2] == (
            "**🛑 not ready to launch — 2 of 4 samples errored**"
        )

    def test_the_digest_says_what_they_did(self) -> None:
        smoke = Smoke(
            outcome=Outcome.FAILED,
            errored=2,
            landed=4,
            failures=(f"2 samples errored the same way — {CLASS}",),
        )

        body = digest_markdown(smoke)

        assert "## what the samples did" in body
        assert CLASS in body
