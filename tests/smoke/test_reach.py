"""What the rehearsal reached, which is not what it found.

**A scan that recorded nothing is indistinguishable from a scan that found nothing.** Both leave an empty findings list, no errors and nothing thrown — so a rehearsal whose scanners never wrote a row reported *rehearsed and ready*, having established nothing at all about the path that is about to review five thousand transcripts. Reproduced: a full two-sample log with no scan rows passed.

**And a task that finished is not a task that ran what was asked.** A log finalizing `success` while holding one of the two samples the slice named lost a sample somewhere that left no error behind, and every count downstream reads the smaller number as the whole truth. Reproduced: a two-sample task with a successful one-sample log passed at `landed=1` against a population of 2.
"""

from pathlib import Path

import pytest
from inspect_steward._evalset.observe import observe_logs
from inspect_steward._smoke import SCAN_COVERAGE, Outcome, Verdict, scan_coverage
from inspect_steward._smoke.digest import Smoke, digest_markdown
from inspect_steward._smoke.run import Plan, conclude, expected, prepare, read
from inspect_steward._smoke.run import short_slices as slices
from inspect_steward._workspace import Workspace, create_workspace

from .._logs import SynthSample, SynthTask, synth_manifest, write_log

SMALL = SynthTask("small", samples=2)
REPEATED = SynthTask("repeated", samples=1, epochs=3)


def planned(tmp_path: Path, *tasks: SynthTask, samples: int = 2) -> Plan:
    create_workspace(tmp_path, git=False)
    return prepare(
        Workspace.at(tmp_path), synth_manifest(list(tasks)), samples=samples, cap=0
    )


def concluded(plan: Plan) -> Smoke:
    return conclude(
        plan, logs=observe_logs(plan.log_dir), capped=False, elapsed=1.0, waived=()
    )


class TestWhatTheScannersReached:
    """`scan_coverage` — the question a census of findings cannot answer."""

    def test_a_rehearsal_whose_scan_recorded_nothing_does_not_pass(
        self, tmp_path: Path
    ) -> None:
        # the whole file: two samples landed, every scanner silent, and the
        # rehearsal used to call that ready
        plan = planned(tmp_path, SMALL)
        write_log(
            Path(plan.log_dir), SMALL, samples=[SynthSample("1"), SynthSample("2")]
        )

        result = concluded(plan)

        assert result.outcome is Outcome.FAILED
        assert SCAN_COVERAGE in result.blocked

    @pytest.mark.parametrize(
        ("reviewed", "landed", "scanning", "verdict"),
        [
            (4, 4, True, Verdict.PASSED),
            (5, 4, True, Verdict.PASSED),
            (0, 4, True, Verdict.FAILED),
            (1, 4, True, Verdict.PASSED),
            (3, 4, True, Verdict.PASSED),
            (0, 0, True, Verdict.UNEXERCISED),
            (0, 4, False, Verdict.UNEXERCISED),
        ],
    )
    def test_the_verdict_over_the_two_numbers(
        self, reviewed: int, landed: int, scanning: bool, verdict: Verdict
    ) -> None:
        check = scan_coverage(reviewed=reviewed, landed=landed, scanning=scanning)

        assert check.verdict is verdict

    def test_a_partial_count_is_reported_rather_than_blocked(self) -> None:
        # a `ScannerConfig.filter` is a SQL clause applied per sample and an
        # excluded transcript is never scanned at all -- no row, no snapshot
        # entry -- and it reaches Steward only as a config hash, so a shortfall
        # cannot be told from a correct one. Blocking would fail every filtered
        # definition on every rehearsal
        check = scan_coverage(reviewed=3, landed=4, scanning=True)

        assert not check.blocks
        assert "3 of 4" in check.detail

    def test_a_run_that_scans_nothing_is_not_a_failure(self) -> None:
        # `unexercised` rather than failed, on the rule the other checks keep:
        # there is no scan path to rehearse, which is not the same as one that
        # does not work
        assert not scan_coverage(reviewed=0, landed=4, scanning=False).blocks

    def test_a_silent_scan_can_be_accepted_by_name(self, tmp_path: Path) -> None:
        # what narrowing the block to zero still costs: a filter selective
        # enough to match none of a two-sample slice records nothing and reads
        # exactly like a broken scan path. A configuration an operator can vouch
        # for and Steward cannot, which is what `--accept` is for
        plan = planned(tmp_path, SMALL)
        write_log(Path(plan.log_dir), SMALL, samples=[SynthSample("1")])

        result = conclude(
            plan,
            logs=observe_logs(plan.log_dir),
            capped=False,
            elapsed=1.0,
            waived=(SCAN_COVERAGE,),
        )

        assert SCAN_COVERAGE not in result.blocked
        assert SCAN_COVERAGE in result.waived_away


class TestWhatTheSliceAskedFor:
    """`short_slices` — finished, and holding less than was asked."""

    def landed(self, plan: Plan, task: SynthTask, records: int) -> list[str]:
        write_log(
            Path(plan.log_dir),
            task,
            samples=[SynthSample(str(one)) for one in range(records)],
        )
        logs = observe_logs(plan.log_dir)
        read_logs, _ = read(logs)
        return slices(plan, logs, read_logs)

    def test_a_successful_log_holding_half_the_slice_is_named(
        self, tmp_path: Path
    ) -> None:
        plan = planned(tmp_path, SMALL)

        lines = self.landed(plan, SMALL, 1)

        key = synth_manifest([SMALL]).tasks[0].key
        assert lines == [f"{key} landed 1 of the 2 samples the rehearsal asked for"]

    def test_and_it_fails_the_rehearsal(self, tmp_path: Path) -> None:
        plan = planned(tmp_path, SMALL)
        write_log(Path(plan.log_dir), SMALL, samples=[SynthSample("1")])

        result = concluded(plan)

        assert result.outcome is Outcome.FAILED
        assert any("1 of the 2 samples" in line for line in result.errors)

    def test_a_log_holding_what_was_asked_says_nothing(self, tmp_path: Path) -> None:
        plan = planned(tmp_path, SMALL)

        assert self.landed(plan, SMALL, 2) == []

    def test_a_task_with_no_log_is_left_to_the_other_vocabulary(
        self, tmp_path: Path
    ) -> None:
        # `unfinished` already says *produced no log*, and one failure reported
        # twice in two wordings is a document a reader has to reconcile
        plan = planned(tmp_path, SMALL)
        read_logs, _ = read(observe_logs(plan.log_dir))

        assert slices(plan, observe_logs(plan.log_dir), read_logs) == []


class TestHowManyTheSliceAsksFor:
    """`expected` — the arithmetic that makes the shortfall computable.

    It reads `min(samples, task.samples) × epochs` for every selection shape there is, and it can, because the capture is untruncated: `task.samples` is what the *run* selects and `selection` always takes at most `samples` from inside it. Epochs multiply rather than truncate — a slice cuts the dataset and not the repeats.
    """

    @pytest.mark.parametrize(
        ("task", "samples", "records"),
        [
            (SynthTask("t", samples=200), 2, 2),
            (SynthTask("t", samples=1), 2, 1),
            (SynthTask("t", samples=200, epochs=3), 2, 6),
            (SynthTask("t", samples=1, epochs=2), 2, 2),
            (SynthTask("t", samples=0), 2, 0),
        ],
    )
    def test_the_records_one_task_owes(
        self, task: SynthTask, samples: int, records: int
    ) -> None:
        manifest = synth_manifest([task])

        assert expected(manifest.tasks[0], samples) == records


class TestWhatTheDigestSays:
    def test_a_shortfall_reaches_what_went_wrong(self, tmp_path: Path) -> None:
        plan = planned(tmp_path, REPEATED, samples=1)
        write_log(Path(plan.log_dir), REPEATED, samples=[SynthSample("1")])

        body = digest_markdown(concluded(plan))

        assert "## what went wrong" in body
        assert "landed 1 of the 3 samples" in body


class TestARehearsalThatRehearsedNothing:
    """An empty capture settles instantly, and every predicate over it is vacuously true.

    `all(...)` over no tasks returns at once, so the watch never waits, no worker starts, every check reads *unexercised*, no error exists — and the journal records a passing smoke at `tasks=landed=population=0`. A definition edited to nothing, or an argument that filtered every task away, was blessed as rehearsed and ready.
    """

    def test_an_empty_capture_does_not_pass(self, tmp_path: Path) -> None:
        plan = planned(tmp_path)

        result = concluded(plan)

        assert result.outcome is Outcome.FAILED
        assert any("enumerated no tasks" in line for line in result.errors)

    def test_the_watch_still_returns_at_once(self, tmp_path: Path) -> None:
        # unchanged, and correct: there is nothing to wait for. What was wrong
        # was the conclusion drawn from it, not the waiting
        from inspect_steward._smoke.run import settled

        plan = planned(tmp_path)

        assert settled(observe_logs(plan.log_dir), plan.manifest) is True


class TestWhichAttemptTheCountsAreOver:
    """Two halves of a slice, landed by two attempts, are not a whole slice.

    A rehearsal spawns once and respawns nothing, but `eval_set()` retries a failed task inside the worker — so a task can land two logs, and summing records across them let two one-record attempts satisfy a two-record rehearsal while the log that is actually the result held one. What the run *is* is the current attempt; the history is a separate question, and `failures` is what answers it.
    """

    def two_attempts(self, tmp_path: Path) -> Plan:
        """One task, two logs: a first attempt that died and the retry that replaced it.

        `eval_set()` retries inside the worker, which is the only way a one-shot
        rehearsal lands two logs for one task. The successful one is the result
        (`observe`: the latest successful attempt wins), and it holds one record
        of the two the slice asked for.
        """
        plan = planned(tmp_path, SMALL)
        write_log(
            Path(plan.log_dir),
            SMALL,
            status="error",
            created="2026-08-30T11:00:00+00:00",
            samples=[SynthSample("1")],
        )
        write_log(
            Path(plan.log_dir),
            SMALL,
            created="2026-08-30T12:00:00+00:00",
            samples=[SynthSample("2")],
        )
        return plan

    def test_the_slice_check_reads_the_current_attempt_alone(
        self, tmp_path: Path
    ) -> None:
        plan = self.two_attempts(tmp_path)
        logs = observe_logs(plan.log_dir)
        read_logs, _ = read(logs)

        assert slices(plan, logs, read_logs) == [
            f"{synth_manifest([SMALL]).tasks[0].key} landed 1 of the 2 samples "
            f"the rehearsal asked for"
        ]

    def test_and_so_does_what_the_digest_reports_as_landed(
        self, tmp_path: Path
    ) -> None:
        # the same population as the slice check and the coverage denominator,
        # so the three numbers in one document cannot disagree
        plan = self.two_attempts(tmp_path)

        assert concluded(plan).landed == 1


class TestWhatTheCapExcuses:
    """The deadline explains a short rehearsal, and nothing else.

    A smoke runs a few samples under a wall-clock cap and stops when it fires,
    mid-sample if that is where it is. Every layer that judged the rehearsal
    read that stop as a defect: `unfinished` reported the cancelled task,
    `short_slices` reported the samples it never reached, and `outcome`
    short-circuited ahead of the checks — so a rehearsal that answered all four
    and landed all but one of its samples was refused, and the remedy on offer
    was to run the whole thing again with a longer clock.

    What must not come back with that fix is the defect underneath: a task that
    failed on its own terms lands a finalized log with an exception in its
    header and nothing beneath it, and excusing that because the rehearsal
    later ran out of time would report a broken definition as ready.

    `scan_coverage` is waived wherever a verdict is the subject, because these
    logs carry no scan rows and the question here is the cap alone.
    """

    def capped(self, plan: Plan, *, waived: tuple[str, ...] = ()) -> Smoke:
        return conclude(
            plan,
            logs=observe_logs(plan.log_dir),
            capped=True,
            elapsed=1.0,
            waived=waived,
        )

    def cancelled_by_the_cap(self, plan: Plan) -> None:
        write_log(
            Path(plan.log_dir),
            SMALL,
            status="error",
            error="Task cancelled by user (abort)",
            error_traceback=(
                "Traceback (most recent call last):\n"
                "inspect_ai._util.exception.TerminateTaskError: "
                "Task cancelled by user (abort)\n"
            ),
            samples=[SynthSample("1")],
        )

    def test_a_task_the_cap_cancelled_is_not_a_failure(self, tmp_path: Path) -> None:
        plan = planned(tmp_path, SMALL)
        self.cancelled_by_the_cap(plan)

        assert self.capped(plan).errors == ()

    def test_and_is_a_failure_when_no_cap_fired(self, tmp_path: Path) -> None:
        # the excusal is the deadline's alone; the same log in a rehearsal that
        # ran its course is a task that died
        plan = planned(tmp_path, SMALL)
        self.cancelled_by_the_cap(plan)

        assert concluded(plan).errors != ()

    def test_a_task_that_died_on_its_own_still_is(self, tmp_path: Path) -> None:
        # the defect `unfinished` exists for: an exception in the header and
        # nothing underneath, which the sample census reads as a clean run of
        # zero samples. A cap later in the rehearsal does not make it clean
        plan = planned(tmp_path, SMALL)
        write_log(
            Path(plan.log_dir),
            SMALL,
            status="error",
            error="No module named 'nowhere'",
            error_traceback=(
                "Traceback (most recent call last):\n"
                "ModuleNotFoundError: No module named 'nowhere'\n"
            ),
        )

        errors = self.capped(plan).errors

        assert len(errors) == 1
        assert "ModuleNotFoundError" in errors[0]

    def test_a_slice_the_cap_cut_short_is_not_reported(self, tmp_path: Path) -> None:
        # the same log the suite above requires a complaint about
        plan = planned(tmp_path, SMALL)
        write_log(Path(plan.log_dir), SMALL, samples=[SynthSample("1")])
        logs = observe_logs(plan.log_dir)
        read_logs, _ = read(logs)

        assert slices(plan, logs, read_logs) != []
        assert slices(plan, logs, read_logs, capped=True) == []

    def test_a_truncated_rehearsal_is_ready_to_launch(self, tmp_path: Path) -> None:
        plan = planned(tmp_path, SMALL)
        write_log(Path(plan.log_dir), SMALL, samples=[SynthSample("1")])

        result = self.capped(plan, waived=(SCAN_COVERAGE,))

        assert result.landed == 1
        assert result.outcome is Outcome.PASSED

    def test_a_cap_that_reached_no_sample_establishes_nothing(
        self, tmp_path: Path
    ) -> None:
        # the one deadline still worth stopping for: no sample landed, so no
        # check could answer and there is nothing to have an opinion about
        plan = planned(tmp_path, SMALL)

        assert self.capped(plan).outcome is Outcome.CAPPED

    def test_nothing_in_the_digest_remarks_on_the_truncation(
        self, tmp_path: Path
    ) -> None:
        # a sample the deadline cut short is the tool working, and there is
        # nothing for a reader to do about it
        plan = planned(tmp_path, SMALL)
        write_log(Path(plan.log_dir), SMALL, samples=[SynthSample("1")])

        rendered = digest_markdown(self.capped(plan, waived=(SCAN_COVERAGE,))).lower()

        assert "rehearsed and ready" in rendered
        assert "truncated" not in rendered
        assert "capped" not in rendered
