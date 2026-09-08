"""Draining the local scan buffer as the run folds, so it never fills the disk.

The buffer scout writes into is local scratch that grows unbounded until the terminal finalize cleans it — which fills the disk on a fleet of concurrent multi-day tasks that never land. So after a fold Steward deletes the buffer files that fold compacted. What makes that safe is two things this file pins against the real scout/inspect-ai: the compacted output keeps a row whose buffer file is gone, and inspect-ai's online resume-skip reads the compacted `transcript_id`s — so a drained-but-folded transcript is neither dropped from the results nor re-scanned on resume.

Offline: real scout recorder + buffer, local parquet writes, no model asked.
"""

from pathlib import Path

import pytest
from inspect_ai._eval.task.scan import _scanned_transcript_ids
from inspect_steward._evalset.manifest import ManifestScan
from inspect_steward._scan import (
    buffer_files,
    drain_buffer,
    initialize_scan,
    scan_dir_location,
    sync_scan,
)

from .test_sync import SCAN_ID, SCANNER, findings, record


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


def test_buffer_files_snapshots_the_per_transcript_files(log_dir: str) -> None:
    # the rows are in the buffer the moment a worker records them, before any
    # fold — one file per transcript, which is what a drain removes
    record(log_dir, uuid="u1", value=True)
    record(log_dir, uuid="u2", value=False)

    files = buffer_files(log_dir=log_dir, scan_id=SCAN_ID)

    assert {path.stem for path in files} == {"u1", "u2"}


def test_buffer_files_is_empty_before_anything_records(log_dir: str) -> None:
    # a bracket laid down but not yet written to has no buffer dir; the snapshot
    # is empty rather than an error, so the trigger simply does not fold
    assert buffer_files(log_dir=log_dir, scan_id=SCAN_ID) == []


def test_a_drained_finding_still_reads_from_the_compacted_output(log_dir: str) -> None:
    # the core safety: once folded, deleting the buffer file loses nothing —
    # the row lives on in the compacted parquet the read consumes
    record(log_dir, uuid="u1", value=True, label="reward_hacking")
    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)

    snapshot = buffer_files(log_dir=log_dir, scan_id=SCAN_ID)
    assert {path.stem for path in snapshot} == {"u1"}
    assert drain_buffer(snapshot) == 1

    assert buffer_files(log_dir=log_dir, scan_id=SCAN_ID) == []
    assert len(findings(log_dir)) == 1


def test_resume_after_a_drain_does_not_re_scan(log_dir: str) -> None:
    # inspect-ai's online resume-skip set unions the buffer stems with the
    # compacted `transcript_id`s, so a drained-but-folded transcript still reads
    # as scanned — a resumed worker skips it rather than paying to scan it again
    record(log_dir, uuid="u1", value=True)
    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)
    drain_buffer(buffer_files(log_dir=log_dir, scan_id=SCAN_ID))

    scan_dir = scan_dir_location(log_dir=log_dir, scan_id=SCAN_ID, scans=None)
    assert "u1" in _scanned_transcript_ids(scan_dir, SCANNER)


def test_a_file_written_during_the_fold_survives_the_drain(log_dir: str) -> None:
    # the snapshot is taken before the fold, so a row a worker writes while the
    # fold runs is not in it and is left for the next fold — never deleted
    # unfolded
    record(log_dir, uuid="u1", value=True)
    snapshot = buffer_files(log_dir=log_dir, scan_id=SCAN_ID)
    record(log_dir, uuid="u2", value=True)
    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)

    drain_buffer(snapshot)

    assert {path.stem for path in buffer_files(log_dir=log_dir, scan_id=SCAN_ID)} == {
        "u2"
    }
    assert {finding.uuid for finding in findings(log_dir)} == {"u1", "u2"}


def test_draining_twice_never_raises(log_dir: str) -> None:
    # missing_ok: a file already gone (a prior drain, a crash mid-drain) costs
    # nothing, so the tend is never failed by the cleanup
    record(log_dir, uuid="u1", value=True)
    sync_scan(log_dir=log_dir, scan_id=SCAN_ID)
    snapshot = buffer_files(log_dir=log_dir, scan_id=SCAN_ID)

    drain_buffer(snapshot)
    drain_buffer(snapshot)  # the same, now-absent files — no error
