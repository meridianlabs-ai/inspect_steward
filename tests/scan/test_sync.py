"""The mid-run fold: rows a worker buffered, readable within a tend.

The one thing here that a synthesized parquet cannot show. `test_findings.py` states the rows and asserts the instances; this states what a *worker* wrote — through scout's own recorder, into scout's own buffer — and asserts that the tend's fold is what makes it readable at all. Without the fold nothing downstream of a scan exists until signoff, so the claim being pinned is the load-bearing one for the whole step.

Offline: a recorder writing a buffer file is a local parquet write, and no model is asked anything.
"""

import json
from pathlib import Path

import pytest
from inspect_ai._util._async import run_coroutine
from inspect_scout import Result
from inspect_scout._recorder.buffer import RecorderBuffer
from inspect_scout._recorder.file import FileRecorder
from inspect_scout._scanner.result import ResultReport
from inspect_scout._transcript.types import TranscriptInfo
from inspect_steward._evalset.instances import Instance
from inspect_steward._evalset.manifest import ManifestScan
from inspect_steward._scan import (
    initialize_scan,
    scan_dir_location,
    scan_findings,
    sync_scan,
)
from inspect_steward._scan.findings import log_key

from .test_findings import attempts

SCAN_ID = "run-1"
SCANNER = "scoring_integrity"
LOG = "/logs/2026-08-31T10-00-00_cybench_aaa.eval"


@pytest.fixture
def log_dir(tmp_path: Path) -> str:
    """A log directory with the scan bracket laid down, as a launch leaves it."""
    logs = tmp_path / "logs"
    logs.mkdir()
    material = ManifestScan(
        spec=None,
        scans=None,
        injected={SCANNER: {"name": f"inspect_steward/{SCANNER}"}},
    )
    initialize_scan(material, log_dir=str(logs), scan_id=SCAN_ID)
    return str(logs)


def record(log_dir: str, *, uuid: str, value: bool, label: str | None = None) -> None:
    """One row, written exactly as a record-only worker writes it."""
    recorder = FileRecorder()
    scan_dir = scan_dir_location(log_dir=log_dir, scan_id=SCAN_ID, scans=None)
    run_coroutine(recorder.attach(scan_dir))
    run_coroutine(
        recorder.record(
            TranscriptInfo(
                transcript_id=uuid,
                source_type="eval_log",
                source_id="eval-1",
                source_uri=LOG,
                task_id="s1",
                task_repeat=1,
            ),
            SCANNER,
            [
                ResultReport(
                    input_type="messages",
                    input_ids=[],
                    input=[],
                    result=Result(
                        value=value,
                        label=label,
                        explanation="it read the grader at [M12]",
                    ),
                    validation=None,
                    error=None,
                    events=[],
                    model_usage={},
                )
            ],
            metrics=None,
        )
    )


def findings(log_dir: str) -> list[Instance]:
    return scan_findings(
        scan_dir_location(log_dir=log_dir, scan_id=SCAN_ID, scans=None),
        scanners=(SCANNER,),
        attempts=attempts(LOG),
    ).instances


def test_a_buffered_row_is_invisible_until_the_tend_folds_it(log_dir: str) -> None:
    # the whole reason the fold exists: workers in selection mode never enter
    # upstream's `scan_context`, so nothing else ever compacts a row
    record(log_dir, uuid="u1", value=True, label="reward_hacking")

    assert findings(log_dir) == []

    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)

    found = findings(log_dir)
    assert len(found) == 1
    assert found[0].class_key == "scan:scoring_integrity:reward_hacking"


def test_the_fold_leaves_the_buffer_where_the_workers_are_still_writing(
    log_dir: str,
) -> None:
    # `complete=False` is the whole safety property: a sibling worker asking
    # `is_recorded` about its own transcript must still get an answer
    record(log_dir, uuid="u1", value=True)
    scan_dir = scan_dir_location(log_dir=log_dir, scan_id=SCAN_ID, scans=None)

    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)

    buffered = RecorderBuffer.buffer_dir(scan_dir) / f"scanner={SCANNER}"
    assert {path.stem for path in buffered.glob("*.parquet")} == {"u1"}
    # and the scan is not claimed complete, so a crash leaves it resumable
    summary = json.loads((Path(scan_dir) / "_summary.json").read_text())
    assert summary["complete"] is False


def test_folding_twice_finds_the_same_row_once(log_dir: str) -> None:
    # every tend folds, so idempotence is the ordinary case rather than an edge
    record(log_dir, uuid="u1", value=True)

    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)
    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)

    assert len(findings(log_dir)) == 1


def test_a_later_row_joins_the_ones_already_folded(log_dir: str) -> None:
    # the merge with the prior compacted output, which is what keeps a run's
    # findings accumulating rather than being replaced by the newest fold
    record(log_dir, uuid="u1", value=True)
    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)
    record(log_dir, uuid="u2", value=True)
    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)

    assert {finding.uuid for finding in findings(log_dir)} == {"u1", "u2"}


def test_a_scanner_that_said_no_folds_and_flags_nothing(log_dir: str) -> None:
    record(log_dir, uuid="u1", value=False)

    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)

    assert findings(log_dir) == []


def test_the_log_key_is_what_joins_the_two_sides(log_dir: str) -> None:
    # a guard on the join itself, at the one place both spellings are real:
    # the recorder wrote `source_uri`, and the lookup is keyed on the basename
    assert log_key(LOG) == "2026-08-31T10-00-00_cybench_aaa.eval"
