"""A scan finding, from a worker's buffered row to the decision it becomes.

Layer 1 like the rest of the item suite, and the whole point of step 30 is that this file is short: the rows are real (scout's own recorder wrote them), the fold is the tend's real fold, the window comes out of the real journal — and *nothing* between the class key and the signoff gate was written for scanning. What is asserted here is that the general machinery holds a finding as well as it holds an error, plus the two things that are genuinely new: the notification hold, and what the signer is told about dismissals.
"""

import json
import os
import traceback
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from inspect_ai._util._async import run_coroutine
from inspect_scout import Result
from inspect_scout._recorder.file import FileRecorder
from inspect_scout._scanner.result import Error, ResultReport
from inspect_scout._transcript.types import TranscriptInfo
from inspect_steward._cli.main import steward
from inspect_steward._evalset.classify import scan_class
from inspect_steward._evalset.manifest import ManifestScan, write_manifest
from inspect_steward._scan import initialize_scan, scan_dir_location, sync_scan
from inspect_steward._schedule import SpawnTask
from inspect_steward._signoff import OPEN_WINDOW, UNREAD, check
from inspect_steward._tend import Level, Owner, Verdict, collect_markdown, turn_post
from inspect_steward._tend.coverage import Coverage, TaskCoverage
from inspect_steward._tend.items import (
    ANOMALY,
    SIGNOFF_READY,
    UNREADABLE,
    Item,
)
from inspect_steward._tend.notify import HOLD_TENDS, UNATTENDED_INTERVALS
from inspect_steward._tend.render import coverage_note
from inspect_steward._worker import record_intent, record_launched
from inspect_steward._worker.inflight import resolve_inflight
from inspect_steward._workspace import (
    COLLECTED,
    DEFAULT_TEND_INTERVAL,
    RULING,
    Workspace,
    append_event,
    create_workspace,
)

from .._logs import SynthSample, SynthTask, write_log
from ..schedule.test_tend import observations, prepared, turn

SCAN_ID = "run-1"
SCANNER = "scoring_integrity"

MATERIAL = ManifestScan(
    spec=None,
    scans=None,
    injected={SCANNER: {"name": f"inspect_steward/{SCANNER}"}},
)


TASK = SynthTask("probe", samples=4)
CLASS = scan_class(
    SCANNER, "reward_hacking", task=TASK.name, identifier=TASK.identifier
)


def scanning(
    root: Path,
    *,
    flagged: int = 2,
    label: str | None = "reward_hacking",
    land: bool = True,
) -> Workspace:
    """A one-task run whose landed log has real scan rows recorded against it.

    The fixture folds rather than leaving it to the turn, because at this point in a run every worker has been reaped and the tend's fold is correctly a no-op (`test_sync.py` owns the fold's own behaviour). What is under test here is everything downstream of a folded row.

    `land=False` leaves the log unwritten, so a first turn can establish the completion baseline the `finished` diff is taken against — a task that was already complete before Steward ever looked is deliberately not news.
    """
    workspace, manifest = prepared(root, [TASK])
    write_manifest(
        manifest.model_copy(update={"scan": MATERIAL, "eval_set_id": SCAN_ID}),
        workspace.manifest,
    )
    initialize_scan(MATERIAL, log_dir=str(workspace.logs), scan_id=SCAN_ID)
    if land:
        land_it(workspace, flagged=flagged, label=label)
    return workspace


def land_it(
    workspace: Workspace, *, flagged: int = 2, label: str | None = "reward_hacking"
) -> None:
    """The task's log arrives, with its scan rows recorded and folded."""
    log = write_log(workspace.logs, TASK)
    for index in range(flagged):
        record(workspace, str(log), uuid=f"u{index}", value=True, label=label)
    # one honest sample, so a run is never all-flagged by construction
    record(workspace, str(log), uuid="clean", value=False, label=None)
    sync_scan(log_dir=str(workspace.logs), scan_id=SCAN_ID)


def thrown(exception: Exception) -> str:
    """A real `traceback.format_exc()`, which is the only error column that classes.

    Raised and caught rather than hand-written, because `scan_error_class` parses what Python actually prints and a plausible-looking string is the way that test stops testing anything. The raising frame is this file, so every class composed here shares one frame and the exception type is what tells them apart.
    """
    try:
        raise exception
    except Exception:
        return traceback.format_exc()


def record(
    workspace: Workspace,
    log: str,
    *,
    uuid: str,
    value: bool | None,
    label: str | None,
    error: str | None = None,
    stack: str = "",
    scanner: str = SCANNER,
) -> None:
    """One row, written exactly as a record-only worker writes it.

    `error` is the shape a scanner that threw leaves behind: no result at all, and the exception in its place.
    """
    recorder = FileRecorder()
    scan_dir = scan_dir_location(
        log_dir=str(workspace.logs), scan_id=SCAN_ID, scans=None
    )
    run_coroutine(recorder.attach(scan_dir))
    run_coroutine(
        recorder.record(
            TranscriptInfo(
                transcript_id=uuid,
                source_type="eval_log",
                source_id="eval-1",
                source_uri=log,
                task_id=uuid,
                task_repeat=1,
            ),
            scanner,
            [
                ResultReport(
                    input_type="messages",
                    input_ids=[],
                    input=[],
                    result=None
                    if error is not None
                    else Result(
                        value=value,
                        label=label,
                        explanation="it read the grader at [M12]",
                    ),
                    validation=None,
                    error=None
                    if error is None
                    else Error(
                        transcript_id=uuid,
                        scanner=scanner,
                        error=error,
                        traceback=stack,
                        refusal=False,
                    ),
                    events=[],
                    model_usage={},
                )
            ],
            metrics=None,
        )
    )


def anomaly_items(workspace: Workspace) -> list[Item]:
    return [item for item in turn(workspace).items if item.kind == ANOMALY]


def parquet(workspace: Workspace) -> Path:
    return (
        Path(
            scan_dir_location(log_dir=str(workspace.logs), scan_id=SCAN_ID, scans=None)
        )
        / f"{SCANNER}.parquet"
    )


def corrupt(workspace: Workspace) -> None:
    """A compacted parquet nothing can open, which is one file both halves read.

    The fold merges the buffer *into* it and the census projects columns *out* of it, so one damaged file is the whole of both failures — which is the shape a store that goes away has too.
    """
    parquet(workspace).write_bytes(b"not a parquet at all")


def repair(workspace: Workspace) -> None:
    parquet(workspace).unlink()


def collected(workspace: Workspace) -> None:
    """Say an agent is attached, which is the whole of the hold's first condition."""
    append_event(workspace.journal, COLLECTED, position=0)


def departed(workspace: Workspace) -> None:
    """A worker the record accounts for and the process table does not.

    Which is the state a tend finds between a worker exiting and the reap that follows it — and the one turn that must fold, because the last of its rows landed after the previous fold.
    """
    selection = workspace.workers / "w1.json"
    selection.parent.mkdir(parents=True, exist_ok=True)
    selection.write_text("{}", encoding="utf-8")
    record_intent(
        workspace.inflight,
        worker="w1",
        tasks=[
            SpawnTask(
                identifier=TASK.identifier,
                key="probe",
                resume=None,
                attempt=1,
                reason=None,
            )
        ],
        selection=selection,
        argv=["true"],
        cwd=str(workspace.root),
        log_dir=str(workspace.logs),
    )
    record_launched(workspace.inflight, worker="w1", pid=1)


def rule(
    workspace: Workspace, disposition: str, class_key: str = CLASS, **fields: Any
) -> None:
    payload: dict[str, Any] = {
        "class": class_key,
        "disposition": disposition,
        "reason": "the model tried to read the grader and failed",
        "by": "kaia",
        **fields,
    }
    append_event(workspace.journal, RULING, **payload)


def test_a_flagged_sample_is_the_agents_investigation(tmp_path: Path) -> None:
    workspace = scanning(tmp_path)

    items = anomaly_items(workspace)

    assert len(items) == 1
    item = items[0]
    assert item.owner is Owner.AGENT
    assert item.level is Level.ATTENTION
    assert item.subject == CLASS
    # the label is the readable half, not the scanner: every finding in a run
    # would otherwise carry the same word
    assert item.id.startswith("anomaly:reward_hacking:")
    assert "2 samples flagged for scoring integrity" in item.summary
    assert item.action == f"steward investigate '{CLASS}'"
    # an ack cannot close it — an anomaly closes on a ruling and nothing else
    assert not item.acknowledgeable


def test_a_scanner_that_said_no_opens_nothing(tmp_path: Path) -> None:
    workspace = scanning(tmp_path, flagged=0)

    assert anomaly_items(workspace) == []


def test_the_window_absorbs_rather_than_reopening_every_turn(tmp_path: Path) -> None:
    # the census recomposes from the parquet every turn, so idempotence here is
    # the ordinary case and a regression would double the count hourly
    workspace = scanning(tmp_path)
    turn(workspace)

    result = turn(workspace)

    assert observations(workspace)[-1]["anomalies"] == {CLASS: 2}
    assert result.anomalies.open[0].evidence.count == 2


def test_a_dismissal_closes_the_window_and_leaves_the_reason(tmp_path: Path) -> None:
    workspace = scanning(tmp_path)
    turn(workspace)
    rule(workspace, "dismiss")

    result = turn(workspace)

    assert result.anomalies.open == ()
    settled = result.anomalies.settled[0]
    assert settled.ruling is not None
    assert settled.ruling.reason == "the model tried to read the grader and failed"


def test_signoff_waits_on_an_untriaged_flag_and_returns_on_the_ruling(
    tmp_path: Path,
) -> None:
    # decision 2b: the window opens on detection, so a flag nobody has looked
    # at is a hole in the record and the gate says so
    workspace = scanning(tmp_path)

    assert not [item for item in turn(workspace).items if item.kind == SIGNOFF_READY]

    rule(workspace, "dismiss")
    result = turn(workspace)

    ready = [item for item in result.items if item.kind == SIGNOFF_READY]
    assert len(ready) == 1
    assert result.verdict is Verdict.COMPLETE


def test_the_signer_is_told_what_was_dismissed(tmp_path: Path) -> None:
    # decision 7a: a dismissal is not a caveat and reaches `anomalies.md`
    # nowhere, but *the model tried to read the grader* is something the operator
    # signing wants to have been told
    workspace = scanning(tmp_path)
    turn(workspace)
    rule(workspace, "dismiss")

    ready = [item for item in turn(workspace).items if item.kind == SIGNOFF_READY]

    assert "2 scan findings were looked at and dismissed" in ready[0].summary


@pytest.mark.parametrize(
    ("disposition", "bucket", "note"),
    [
        # an excluded sample leaves the denominator; a zeroed one stays in it
        # and counts as a miss, which is the whole difference between the two
        ("exclude", "excluded", "Scores are over 2 of 4 samples (2 excluded)."),
        ("zero", "zeroed", "Scores are over 4 of 4 samples (2 zeroed)."),
    ],
)
def test_a_confirmed_hack_is_a_sample_the_scores_are_no_longer_over(
    disposition: str, bucket: str, note: str, tmp_path: Path
) -> None:
    # §12.6.1's validity route, and the reason `honest()` admits the sample
    # marks here at all: a confirmed reward hack is a sample excluded from
    # scoring exactly as an excluded timeout is, and a ruling that moved no
    # denominator would be a decision the operator made and the numbers never
    # heard
    workspace = scanning(tmp_path)
    turn(workspace)
    rule(workspace, disposition, effect="2 samples are out of the scores")

    result = turn(workspace)

    assert getattr(result.dispositions, bucket) == 2
    caveats = workspace.anomalies.read_text(encoding="utf-8")
    # the denominator moved, and the entry names them as samples rather than as
    # attempts — one flagged sample is one row out of the scores, not one log
    assert note in caveats
    assert "- **Samples** — `u0:1`, `u1:1`" in caveats


def test_an_accepted_finding_is_a_caveat_and_a_dismissed_one_is_not(
    tmp_path: Path,
) -> None:
    workspace = scanning(tmp_path)
    turn(workspace)
    rule(workspace, "accept", effect="2 samples scored as recorded")

    turn(workspace)

    caveats = (workspace.anomalies).read_text(encoding="utf-8")
    assert CLASS in caveats
    assert "2 samples scored as recorded" in caveats


def test_a_dismissed_finding_leaves_no_caveat(tmp_path: Path) -> None:
    workspace = scanning(tmp_path)
    turn(workspace)
    rule(workspace, "dismiss")

    turn(workspace)

    assert CLASS not in (workspace.anomalies).read_text(encoding="utf-8")


class TestAScannerThatCouldNotScan:
    """A transcript nobody scanned is not a transcript that came back clean.

    The two are indistinguishable in the findings — an errored row is read past exactly as a `false` one is — so a run whose every scan threw would read as a run with nothing to report, and the signature would say so.

    **It is a window like any other**, which is what this class asserts. It was an acknowledgeable item and a blocker of its own for exactly one step; both are gone, because the decision an operator is being asked for is *these transcripts carry no verdict and the results stand anyway* — a ruling with a disposition on it, not a wave-past — and two machines refusing over one fact is the shape that lets one of them drift.
    """

    def erroring(self, workspace: Workspace, count: int = 1) -> str:
        log = write_log(workspace.logs, TASK)
        stack = thrown(TimeoutError("the grader model timed out"))
        for index in range(count):
            record(
                workspace,
                str(log),
                uuid=f"e{index}",
                value=None,
                label=None,
                error="the grader model timed out",
                stack=stack,
            )
        sync_scan(log_dir=str(workspace.logs), scan_id=SCAN_ID)
        return f"scanerror:{SCANNER}:TimeoutError@anomaly/test_scan_items.py:thrown"

    def test_the_transcripts_nothing_read_are_one_class(self, tmp_path: Path) -> None:
        workspace = scanning(tmp_path, land=False)
        class_key = self.erroring(workspace, count=2)

        result = turn(workspace)

        assert [window.class_key for window in result.anomalies.open] == [class_key]
        assert result.anomalies.open[0].evidence.count == 2
        items = [item for item in result.items if item.kind == ANOMALY]
        assert items[0].owner is Owner.AGENT
        assert "2 transcripts could not be scanned" in items[0].summary
        # the exception is the recognisable half here, exactly as the label is
        # for a finding — every scan error in a run would otherwise read alike
        assert items[0].id.startswith("anomaly:TimeoutError:")
        # and an ack cannot close it, which is the whole of the retirement
        assert not items[0].acknowledgeable

    def test_it_refuses_the_signature_once_and_through_the_window(
        self, tmp_path: Path
    ) -> None:
        # not twice: an `unscanned` blocker beside the open window would be two
        # refusals over one fact, and the one an operator cleared first would
        # decide which of them they heard about
        workspace = scanning(tmp_path, land=False)
        class_key = self.erroring(workspace)

        blockers = check(turn(workspace), None)

        assert [blocker.kind for blocker in blockers] == [OPEN_WINDOW]
        assert class_key in blockers[0].summary

    def test_accepting_it_signs_over_it_and_leaves_the_caveat(
        self, tmp_path: Path
    ) -> None:
        # the gate's ordinary shape rather than an exception to it: a hole with
        # a name on it is signed over, and one nobody named is refused
        workspace = scanning(tmp_path, land=False)
        class_key = self.erroring(workspace)
        turn(workspace)
        rule(
            workspace,
            "accept",
            class_key=class_key,
            reason="one grader timeout in five hundred; the rest scanned",
            effect="1 transcript carries no verdict either way",
        )

        result = turn(workspace)

        assert check(result, None) == []
        caveats = workspace.anomalies.read_text(encoding="utf-8")
        assert class_key in caveats
        assert "1 transcript carries no verdict either way" in caveats

    def test_its_members_are_named_as_samples(self, tmp_path: Path) -> None:
        # it has one instance per transcript, so the entry says *samples* — the
        # half of `SAMPLE_SHAPED` this kind keeps, where the marks are the half
        # it refuses
        workspace = scanning(tmp_path, land=False)
        class_key = self.erroring(workspace)
        turn(workspace)
        rule(
            workspace,
            "accept",
            class_key=class_key,
            reason="one grader timeout in five hundred",
            effect="1 transcript carries no verdict either way",
        )

        result = turn(workspace)

        caveats = workspace.anomalies.read_text(encoding="utf-8")
        assert "- **Samples** —" in caveats
        # and nothing was marked: the residue is an absent verdict, not a bad
        # row, so no denominator moved
        assert (result.dispositions.excluded, result.dispositions.zeroed) == (0, 0)

    def test_a_scanner_that_answered_every_transcript_says_nothing(
        self, tmp_path: Path
    ) -> None:
        workspace = scanning(tmp_path)

        result = turn(workspace)

        assert not [
            window for window in result.anomalies.open if window.kind == "scanerror"
        ]


class TestCoverage:
    """Recorded rows against landed samples — how much of what landed was looked at.

    **The number a findings list cannot supply.** *Every scanner answered and found nothing* and *the scanners never ran* produce the same empty list, and a signature over the second says *nothing was flagged* about transcripts nothing looked at.

    The resume is where it gets hard, and the two cases below are the two halves of it. A retry writes a new log carrying the samples that already succeeded — same uuid, new file — while their scan rows go on naming the superseded one; and it re-runs the failures under *fresh* uuids that nothing has scanned yet. Count rows per log and the first half vanishes; count them all and the second half is hidden behind samples that are no longer in the results.
    """

    FIRST = "2026-08-23T19:00:00+00:00"
    SECOND = "2026-08-23T20:00:00+00:00"

    def landed(
        self, workspace: Workspace, samples: list[SynthSample], **fields: Any
    ) -> Path:
        return write_log(workspace.logs, TASK, samples=samples, **fields)

    def scanned(
        self, workspace: Workspace, log: Path, samples: list[SynthSample]
    ) -> None:
        for sample in samples:
            record(workspace, str(log), uuid=sample.uuid, value=False, label=None)
        sync_scan(log_dir=str(workspace.logs), scan_id=SCAN_ID)

    def whole(self) -> list[SynthSample]:
        return [SynthSample(f"s{n}") for n in range(4)]

    def test_a_task_whose_every_sample_was_scanned_reports_full(
        self, tmp_path: Path
    ) -> None:
        workspace = scanning(tmp_path, land=False)
        samples = self.whole()
        self.scanned(workspace, self.landed(workspace, samples), samples)

        result = turn(workspace)

        assert result.coverage.by_task[TASK.identifier] == TaskCoverage(
            scanned=4, landed=4
        )
        assert result.coverage.gap == 0
        assert coverage_note(result) is None

    def test_a_worker_that_died_between_logging_and_scanning_shows_a_gap(
        self, tmp_path: Path
    ) -> None:
        # the case the whole column exists for: the samples are in the results
        # and no scanner ever read two of them
        workspace = scanning(tmp_path, land=False)
        samples = self.whole()
        self.scanned(workspace, self.landed(workspace, samples), samples[:2])

        result = turn(workspace)

        assert result.coverage.by_task[TASK.identifier] == TaskCoverage(
            scanned=2, landed=4
        )
        note = coverage_note(result)
        assert note is not None
        assert "over 2 of 4 samples (2 not yet scanned)" in note
        assert "| 2/4 |" in collect_markdown(result)

    def test_a_reused_sample_scanned_under_the_old_log_still_counts(
        self, tmp_path: Path
    ) -> None:
        # its row names the superseded file and it is in the results all the
        # same — a per-log count would report a gap that does not exist and
        # send somebody looking for a scanner that never failed
        workspace = scanning(tmp_path, land=False)
        first = self.whole()
        old = self.landed(workspace, first, created=self.FIRST, status="error")
        rerun = [SynthSample("s2", attempt=2), SynthSample("s3", attempt=2)]
        new = self.landed(workspace, [*first[:2], *rerun], created=self.SECOND)
        self.scanned(workspace, old, first)
        self.scanned(workspace, new, rerun)

        result = turn(workspace)

        assert result.coverage.by_task[TASK.identifier] == TaskCoverage(
            scanned=4, landed=4
        )

    def test_a_rerun_sample_nothing_has_scanned_yet_is_a_gap(
        self, tmp_path: Path
    ) -> None:
        # and this is what the intersection earns. Four rows were recorded and
        # four samples landed, so counting rows says *fully scanned* — but two
        # of those rows are about samples the retry replaced, and the two that
        # replaced them nothing has read
        workspace = scanning(tmp_path, land=False)
        first = self.whole()
        old = self.landed(workspace, first, created=self.FIRST, status="error")
        rerun = [SynthSample("s2", attempt=2), SynthSample("s3", attempt=2)]
        self.landed(workspace, [*first[:2], *rerun], created=self.SECOND)
        self.scanned(workspace, old, first)

        result = turn(workspace)

        assert result.coverage.by_task[TASK.identifier] == TaskCoverage(
            scanned=2, landed=4
        )

    def test_a_current_log_that_will_not_read_reports_unknown_not_full(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the gap above with its one read failing. Falling back to counting rows
        # here says 4 of 4 — a run reported as fully scanned on the strength of
        # rows about samples it replaced, which is the reassuring answer and the
        # wrong one. Unknown is neither, and every surface has to say so
        workspace = scanning(tmp_path, land=False)
        first = self.whole()
        old = self.landed(workspace, first, created=self.FIRST, status="error")
        rerun = [SynthSample("s2", attempt=2), SynthSample("s3", attempt=2)]
        self.landed(workspace, [*first[:2], *rerun], created=self.SECOND)
        self.scanned(workspace, old, first)

        def unreadable(location: str) -> frozenset[str] | None:
            return None

        monkeypatch.setattr("inspect_steward._tend.turn.sample_uuids", unreadable)

        result = turn(workspace)

        entry = result.coverage.by_task[TASK.identifier]
        assert not entry.known
        assert not entry.complete
        assert result.coverage.unverified == (TASK.identifier,)
        # and it is out of the run's totals, so the gap stays a counted number
        assert (result.coverage.scanned, result.coverage.landed) == (0, 0)
        assert "| ?/4 |" in collect_markdown(result)
        note = coverage_note(result)
        assert note is not None
        assert "could not be checked for 1 task" in note
        # including the file somebody quotes into a write-up
        assert "could not establish how many of 4 transcripts" in (
            workspace.analysis.read_text(encoding="utf-8")
        )

    def test_a_finding_on_a_reused_sample_is_still_in_the_results(
        self, tmp_path: Path
    ) -> None:
        # the same fix from the other side: the finding's row names the
        # superseded log, and under a location test it would leave the census's
        # narrowing silently — taking with it the ruling meant to cover it
        workspace = scanning(tmp_path, land=False)
        first = self.whole()
        old = self.landed(workspace, first, created=self.FIRST, status="error")
        rerun = [SynthSample("s2", attempt=2), SynthSample("s3", attempt=2)]
        self.landed(workspace, [*first[:2], *rerun], created=self.SECOND)
        record(
            workspace, str(old), uuid=first[0].uuid, value=True, label="reward_hacking"
        )
        sync_scan(log_dir=str(workspace.logs), scan_id=SCAN_ID)
        turn(workspace)
        rule(workspace, "exclude", effect="1 sample is out of the scores")

        result = turn(workspace)

        assert result.dispositions.excluded == 1
        assert len(result.dispositions.affected[CLASS]) == 1
        assert CLASS in workspace.anomalies.read_text(encoding="utf-8")

    def test_a_run_that_scans_nothing_reports_no_coverage_at_all(
        self, tmp_path: Path
    ) -> None:
        # and the column and the note go with it: a run with no scan material
        # has no question here to answer
        workspace, _ = prepared(tmp_path, [TASK])
        write_log(workspace.logs, TASK)

        result = turn(workspace)

        assert result.coverage == Coverage()
        assert coverage_note(result) is None
        assert "| scanned |" not in collect_markdown(result)


class TestTheFoldsCadence:
    """When the tend folds at all, which is the difference between a finding and silence."""

    def test_a_quiescent_run_does_not_fold(self, tmp_path: Path) -> None:
        # a mid-run fold re-compacts the whole buffer, so a settled campaign
        # must not pay for one every ten minutes — a row nothing has folded
        # stays unread until a worker moves again
        workspace = scanning(tmp_path, land=False)
        log = write_log(workspace.logs, TASK)
        record(workspace, str(log), uuid="u0", value=True, label="reward_hacking")

        assert turn(workspace).anomalies.open == ()

    def test_a_departed_worker_is_folded_before_it_is_reaped(
        self, tmp_path: Path
    ) -> None:
        # the case the gate exists for: the last worker's final rows land after
        # the previous fold, and the turn that reaps it is the one that must
        # pick them up
        workspace = scanning(tmp_path, land=False)
        log = write_log(workspace.logs, TASK)
        record(workspace, str(log), uuid="u0", value=True, label="reward_hacking")
        departed(workspace)

        result = turn(workspace)

        assert [anomaly.class_key for anomaly in result.anomalies.open] == [CLASS]

    def test_a_scan_directory_that_will_not_read_costs_the_turn_nothing(
        self, tmp_path: Path
    ) -> None:
        # it costs the turn nothing and it is *said*: a file nobody could open
        # is not a scanner that found nothing, and the difference is the whole
        # of what a signature would otherwise be taken over
        workspace = scanning(tmp_path)
        corrupt(workspace)

        result = turn(workspace)

        assert result.anomalies.open == ()
        assert result.summary.states["complete"] == 1
        unread = [item for item in result.items if item.kind == UNREADABLE]
        assert [item.id for item in unread] == [f"unreadable:{SCANNER}.parquet"]
        assert "scoring_integrity's scan results" in unread[0].summary

    def test_scan_results_nobody_could_read_refuse_the_signature(
        self, tmp_path: Path
    ) -> None:
        workspace = scanning(tmp_path)
        corrupt(workspace)

        blockers = check(turn(workspace), None)

        assert [blocker.kind for blocker in blockers] == [UNREAD]

    def test_a_fold_that_failed_on_the_departure_turn_is_retried(
        self, tmp_path: Path
    ) -> None:
        """The hole the cheap gate would otherwise have, and it ends at the signature.

        A fold that fails on the turn a worker departs is a fold that never happens: the reap lands, `running or departed` stops firing, and the rows sit in the buffer through every later tend — until signoff's terminal finalize folds them, after the gate has passed.
        """
        workspace = scanning(tmp_path, land=False)
        log = write_log(workspace.logs, TASK)
        record(workspace, str(log), uuid="u0", value=True, label="reward_hacking")
        corrupt(workspace)
        departed(workspace)

        assert turn(workspace).anomalies.open == ()

        # the worker is reaped, so nothing is running and nothing has departed:
        # the open episode is the only thing that can bring the fold back
        inflight = resolve_inflight(workspace.inflight, workspace.workers)
        assert not inflight.running and not inflight.departed
        repair(workspace)
        result = turn(workspace)

        assert [anomaly.class_key for anomaly in result.anomalies.open] == [CLASS]


class TestWaitingToLand:
    """A scan window on a running task is nobody's work yet.

    The finding is decided when the task is done — every window the task has, put to the operator at once — so until then the window is on the record and out of the queue, and the listing says so in words rather than showing it as open.
    """

    def running(self, tmp_path: Path) -> Workspace:
        workspace = scanning(tmp_path, land=False)
        log = write_log(workspace.logs, TASK, status="started", total=4, completed=2)
        record(workspace, str(log), uuid="u0", value=True, label="reward_hacking")
        sync_scan(log_dir=str(workspace.logs), scan_id=SCAN_ID)
        return workspace

    def test_the_window_opens_and_raises_nothing(self, tmp_path: Path) -> None:
        workspace = self.running(tmp_path)

        result = turn(workspace)

        assert CLASS in [anomaly.class_key for anomaly in result.anomalies.open]
        assert [item for item in result.items if item.subject == CLASS] == []
        assert "waiting for the task to land — 1 instance" in collect_markdown(result)

    def test_landing_puts_it_in_the_queue(self, tmp_path: Path) -> None:
        workspace = self.running(tmp_path)
        turn(workspace)
        # the same attempt, now complete: the rows already recorded against
        # its log are the task's findings
        write_log(workspace.logs, TASK)

        result = turn(workspace)

        (item,) = [item for item in result.items if item.subject == CLASS]
        assert item.owner is Owner.AGENT
        assert item.action == f"steward investigate '{CLASS}'"
        assert "waiting for the task to land" not in collect_markdown(result)


class TestTheHold:
    """Decision 4/5/6: a landed task with a finding waits for the agent, briefly.

    Every case runs the same two turns — one before the log lands, so there is a completion baseline for the `finished` diff to be taken against, and one after. What differs is only whether the second turn announces the finish.
    """

    def landing(self, tmp_path: Path, **kwargs: Any) -> Workspace:
        workspace = scanning(tmp_path, land=False)
        turn(workspace)
        land_it(workspace, **kwargs)
        return workspace

    def test_a_finish_waits_while_an_agent_is_attached(self, tmp_path: Path) -> None:
        workspace = self.landing(tmp_path)
        collected(workspace)

        result = turn(workspace)

        assert result.held == frozenset({TASK.identifier})
        assert result.finished == []
        # and the completion is *unspent*: the next turn must be able to
        # announce it, so it is kept out of the set the diff is taken against
        assert observations(workspace)[-1]["complete"] == []

    def test_the_hold_is_a_deferral_and_not_a_suppression(self, tmp_path: Path) -> None:
        workspace = self.landing(tmp_path)
        collected(workspace)
        turn(workspace)

        result = turn(workspace)

        assert result.held == frozenset({TASK.identifier})
        assert result.finished == []
        assert observations(workspace)[-1]["complete"] == []

    def test_a_ruling_releases_it(self, tmp_path: Path) -> None:
        workspace = self.landing(tmp_path)
        collected(workspace)
        turn(workspace)
        rule(workspace, "dismiss")

        result = turn(workspace)

        assert result.held == frozenset()
        assert result.finished == [TASK.identifier]

    def test_nobody_attached_means_nothing_is_held(self, tmp_path: Path) -> None:
        # a hold with nobody to release it is silence; the finding escalates on
        # `_unattended`'s own horizon instead
        workspace = self.landing(tmp_path)

        result = turn(workspace)

        assert result.held == frozenset()
        assert result.finished == [TASK.identifier]

    def test_a_task_with_no_findings_is_never_held(self, tmp_path: Path) -> None:
        workspace = self.landing(tmp_path, flagged=0)
        collected(workspace)

        result = turn(workspace)

        assert result.held == frozenset()
        assert result.finished == [TASK.identifier]

    def test_the_horizon_releases_an_agent_that_stopped_answering(
        self, tmp_path: Path
    ) -> None:
        workspace = self.landing(tmp_path)
        collected(workspace)
        turn(workspace)

        # the completion has been waiting more than `HOLD_TENDS` cadences,
        # which is the moment the hold gives up and posts it regardless
        _age_the_wait(workspace, seconds=HOLD_TENDS * DEFAULT_TEND_INTERVAL + 60)
        result = turn(workspace)

        assert result.held == frozenset()
        assert result.finished == [TASK.identifier]

    def test_a_proposal_releases_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hold is for the agent's proposals, so it ends the moment they exist.

        The finish then goes out carrying them, which is what it was waiting
        for; making it wait the whole horizon would post the proposals on one
        turn and the finish they belong to on another.
        """
        create_workspace(tmp_path, git=False)
        workspace = self.landing(tmp_path)
        collected(workspace)
        turn(workspace)
        monkeypatch.chdir(workspace.root)
        proposed = CliRunner().invoke(
            steward,
            ["propose", CLASS, "--action", "zero", "--reason", "read the grader"],
        )
        assert proposed.exit_code == 0, proposed.output

        result = turn(workspace)

        assert result.held == frozenset()
        assert result.finished == [TASK.identifier]

    def test_a_task_that_lands_late_into_an_old_class_is_held_too(
        self, tmp_path: Path
    ) -> None:
        """A window opens on the first flagged sample, so its age says nothing about the finish.

        A task that flags its first sample early and lands hours later has a window older than the hold would ever wait, and a completion that arrived just now; the hold counts from the log, not the window.
        """
        workspace = self.landing(tmp_path)
        collected(workspace)
        turn(workspace)
        # the window has been open longer than the hold would ever wait, and
        # the completion in front of us arrived just now
        _age(
            workspace,
            seconds=HOLD_TENDS * DEFAULT_TEND_INTERVAL + 60,
            type="opened",
            **{"class": CLASS},
        )

        result = turn(workspace)

        assert result.held == frozenset({TASK.identifier})
        assert result.finished == []

    def test_a_finish_already_announced_is_not_taken_back(self, tmp_path: Path) -> None:
        """The hold defers an announcement; it cannot retract one.

        A fold that failed on the departure turn records and announces the completion, and the retry a turn later discovers the finding. Withholding then removes the task from the baseline the next diff is taken against — so ruling the finding announces the same finish a second time.
        """
        workspace = scanning(tmp_path, land=False)
        log = write_log(workspace.logs, TASK)
        turn(workspace)

        # the completion is spent: recorded, and the diff is taken against it
        assert TASK.identifier in observations(workspace)[-1]["complete"]

        # and only now does the fold that was owed find the finding
        record(workspace, str(log), uuid="u0", value=True, label="reward_hacking")
        sync_scan(log_dir=str(workspace.logs), scan_id=SCAN_ID)
        collected(workspace)
        result = turn(workspace)

        assert result.held == frozenset()
        assert result.finished == []
        assert TASK.identifier in observations(workspace)[-1]["complete"]
        # and the finding still reaches somebody — it is the finish that is
        # not news twice, not the flag
        assert [anomaly.class_key for anomaly in result.anomalies.open] == [CLASS]

    def test_the_released_post_says_it_once_in_the_summary(
        self, tmp_path: Path
    ) -> None:
        # decision 6: one line about the post, never a qualifier per task row
        workspace = self.landing(tmp_path)

        post = turn_post(turn(workspace))

        assert post is not None
        said = [line for line in post.lines if "scan findings" in line]
        assert said == ["1 with scan findings nobody has ruled on"]


def test_an_agent_that_goes_quiet_hands_its_investigation_to_the_person(
    tmp_path: Path,
) -> None:
    """The item was offered once, to somebody who is no longer there.

    An agent-owned item reaches the operator by *arriving*, and a scan finding first seen while an agent was attached has already arrived — so without a second way in, the one item nobody picked up is the one item nobody is ever told about.
    """
    workspace = scanning(tmp_path)
    collected(workspace)

    attended = turn_post(turn(workspace))

    # an agent is attached, so the investigation is theirs and the post is
    # silent about it — which is decision 4 working
    assert attended is None or not any("integrity" in line for line in attended.lines)

    # **and the silence outlasted more than one interval**, which is the case a
    # fixed-cadence window loses: the crossing happened three cadences before
    # this turn, so only a turn measuring the gap it is actually answering for
    # can still catch it
    _age_everything(
        workspace, seconds=(UNATTENDED_INTERVALS + 3) * DEFAULT_TEND_INTERVAL
    )
    post = turn_post(turn(workspace))

    assert post is not None
    assert any("flagged for scoring integrity" in line for line in post.lines)


def _age_the_wait(workspace: Workspace, *, seconds: float) -> None:
    """Move back both instants a completion could have been waiting since.

    The clock the hold reads is the task's own log — when the observation says it landed — with the window's opening standing in where the filesystem does not date it. Ageing both is the whole of simulating an agent that went quiet: no sleeping, and nothing patched.
    """
    _age(workspace, seconds=seconds, type="opened", **{"class": CLASS})
    for log in workspace.logs.iterdir():
        if log.suffix in (".eval", ".json"):
            landed = log.stat().st_mtime - seconds
            os.utime(log, (landed, landed))


def _age_everything(workspace: Workspace, *, seconds: float) -> None:
    """Move the whole record back, which is the whole of a long silence.

    Both clocks, deliberately: an agent that stopped collecting while the timer went on firing every ten minutes is a different story from a workspace nothing touched all night, and it is the second one that loses a handoff measured against a nominal cadence.
    """
    _age(workspace, seconds=seconds)


def _age(workspace: Workspace, *, seconds: float, **match: Any) -> None:
    from datetime import datetime, timedelta

    lines = workspace.journal.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        event = json.loads(line)
        if all(event.get(field) == value for field, value in match.items()):
            moved = datetime.fromisoformat(event["ts"]) - timedelta(seconds=seconds)
            event["ts"] = moved.isoformat()
            line = json.dumps(event)
        rewritten.append(line)
    workspace.journal.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


class TestAConfiguredScanWithNoIdentity:
    """*Configured scanning whose directory cannot be found* read as *no scanning at all*.

    The two conditions were one predicate. No `material` means this run scans nothing and an empty census is simply true for it; a missing `scan_id` — `.eval-set-id` gone from the log directory, and no id in the committed manifest — means the scan is configured and nothing can be located. Everything downstream then quietly becomes *there was never anything here*: no census, no coverage, no terminal finalize, and a signature saying nothing was flagged about transcripts nobody could look for.
    """

    def unidentified(self, tmp_path: Path) -> Workspace:
        """A scanning run whose eval-set id has gone missing from both places."""
        workspace, manifest = prepared(tmp_path, [TASK])
        write_manifest(
            manifest.model_copy(update={"scan": MATERIAL}), workspace.manifest
        )
        write_log(workspace.logs, TASK)
        return workspace

    def test_it_is_reported_as_unreadable_rather_than_as_silence(
        self, tmp_path: Path
    ) -> None:
        result = turn(self.unidentified(tmp_path))

        assert result.observed is not None
        assert [entry.what for entry in result.observed.unreadable] == [
            "this run's scan results"
        ]

    def test_and_the_signature_is_refused_over_it(self, tmp_path: Path) -> None:
        # through the vocabulary the gate already refuses on: a hole nobody can
        # size is signed over by naming it, never by not noticing it
        from inspect_steward._signoff import Signoff, signoff

        result = signoff(self.unidentified(tmp_path), by="kaia")

        assert isinstance(result, Signoff)
        assert [blocker.kind for blocker in result.blockers] == [UNREAD]
        assert result.signature is None

    def test_a_run_that_scans_nothing_still_says_nothing(self, tmp_path: Path) -> None:
        # the other half of the predicate, which was and remains correct
        workspace, _ = prepared(tmp_path, [TASK])
        write_log(workspace.logs, TASK)

        observed = turn(workspace).observed
        assert observed is not None and observed.unreadable == []
