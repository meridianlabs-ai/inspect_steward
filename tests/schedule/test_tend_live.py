"""The loop, closed once, on a real eval — started the way an operator starts one.

Everything else about the turn is settled in `test_tend.py` against synthesized
state. What no synthesized state can show is that the four layers agree with
each other about the *same* run: capture writes identifiers and a content hash,
a worker writes a log, and observation matches the two. Each layer is otherwise
tested against a fixture of the next one's shape, and a fixture is a belief
about that shape rather than the shape itself.

**Through `launch` rather than by hand.** This test used to perform launch's
four steps itself — copy the definition, capture, commit, tend — which meant the
composition an operator actually runs was the one thing never exercised against a
real eval. Going through the verb asserts the commit, the arming, and the
journal entry for the same three launches, and it is the only place the *real*
capture and the *real* delta meet each other.

**Budget: three launches** (plan.md §10) — one capture and two workers, in one
test. The re-launch at the end costs a second capture and no workers, which is
what keeps the claim *two captures of one unedited file agree about every
identifier* inside this budget rather than in a test of its own.

Two things the plan sketched are deliberately not here. The no-log stall is not
repeated: `test_tend.py` runs that path with real processes, a real in-flight
record, and the real guard, and paying an eval's startup for it would buy only
the knowledge that `faulty_evalset.py`'s `pre:crash` lands no log, which
`tests/worker/test_faults.py` already establishes. Drift against a real capture
is not its own test either — it is one assertion below, because a capture is
exactly what makes `drift is False` mean anything.
"""

import shutil
from pathlib import Path

import pytest
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_steward._launch import Change, Launch, launch
from inspect_steward._workspace import (
    Workspace,
    create_workspace,
    read_journal,
    read_launched,
)

from ..timer._fake import clear_credentials, fake_cron
from .test_tend import settle, turn

FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"


def test_a_packed_run_lands_every_log_from_one_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`max_workers: 1` — the whole eval set in one worker, which is the point.

    The claim no synthesized state can make: that a selection naming several
    tasks actually runs all of them, that they run *concurrently* rather than
    falling through to `eval()`'s one-at-a-time default, and that the scan and
    the reaper account for one process holding two tasks. The concurrency half
    is what the `max_tasks` override buys and is the reason it is written.

    **Budget: one launch, one worker** — cheaper than the default width, which
    is the whole argument for the feature.
    """
    fake_cron(monkeypatch)
    clear_credentials(monkeypatch)
    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    workspace.directives.write_text("max_workers: 1\n", encoding="utf-8")
    definition = workspace.root / "evalset.py"
    shutil.copy(FIXTURES / "simple_evalset.py", definition)

    started = launch(workspace, definition)
    assert isinstance(started, Launch), f"refused by {started}"
    assert started.turn is not None
    # two tasks, one process: the startup cost the feature exists to cut
    assert len(started.manifest.tasks) == 2
    assert len(started.turn.spawned) == 1
    assert started.turn.summary.workers == 0 and started.turn.summary.spawning == 2

    settle(workspace)
    finished = turn(workspace)

    # both logs land, and the one worker is reaped once
    landed = list_eval_logs(str(workspace.logs))
    assert len(landed) == 2
    assert finished.summary.states["complete"] == 2
    assert len(finished.reaped) == 1

    # and they ran together rather than one after the other. This is the whole
    # reason the override is written: without it `eval_set()`'s own default is
    # never applied in selection mode, and `eval()` falls back to one task at a
    # time. The log is where that decision is recoverable after the fact
    assert [read_eval_log(log.name).eval.config.max_tasks for log in landed] == [2, 2]
    # and the run is converged, not merely quiet: nothing is left to spawn
    assert finished.spawned == [] and finished.queued == []


def test_a_run_converges_and_then_stays_converged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the scheduler's system, faked at the seam all three backends go through:
    # arming for real would load a launch agent — or a systemd timer — pointing
    # at a pytest temp directory into the session running the suite
    fake_cron(monkeypatch)
    # and the credentials the arming shell holds, which a real launch checks
    # against `.env` before it will arm anything
    clear_credentials(monkeypatch)

    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    definition = workspace.root / "evalset.py"
    shutil.copy(FIXTURES / "simple_evalset.py", definition)

    started = launch(workspace, definition)
    assert isinstance(started, Launch), f"refused by {started}"
    settle(workspace)
    finished = turn(workspace)
    again = turn(workspace)

    # every task is new and nothing leaves logs/, so a first launch commits
    # itself: this is the delta the agent is allowed to apply unasked
    assert started.delta.first is True
    assert started.delta.additive is True
    assert len(started.delta.of(Change.ADD)) == 2
    assert started.committed is True

    # capture and drift hash the same file the same way, which is a claim about
    # two modules agreeing and cannot be made against a synthesized manifest
    assert started.turn is not None
    assert started.turn.drift is False
    assert len(started.turn.spawned) == len(started.manifest.tasks) == 2

    # the two things a launch leaves behind that no tend would
    assert started.armed is not None
    assert read_launched(read_journal(workspace.journal).events) is not None

    # the identifiers capture wrote are the ones observation recovered from the
    # logs those workers landed -- the correlation the whole design rests on
    assert sorted(finished.reaped) == sorted(started.turn.spawned)
    assert finished.summary.states["complete"] == 2
    assert finished.spawned == []

    # and a converged run is a fixed point rather than somewhere it passes
    # through: the second tend does nothing, and so would the two hundredth
    assert (again.spawned, again.reaped, again.archived) == ([], [], [])
    assert again.summary.states["complete"] == 2
    assert len(list_eval_logs(str(workspace.logs))) == 2

    # so is a re-launch, which is the amend path's floor: a second capture of
    # the unedited file agrees with the first about every identifier, so the
    # delta is empty and nothing is proposed for the archive. A capture is two
    # subprocesses apart from the first one, which is what makes this a claim
    # about `task_identifier` rather than about a dictionary comparison
    relaunched = launch(workspace, definition)
    assert isinstance(relaunched, Launch)
    assert (relaunched.delta.empty, relaunched.delta.first) == (True, False)
    assert relaunched.committed is True
    assert relaunched.turn is not None
    assert (relaunched.turn.spawned, relaunched.turn.archived) == ([], [])
    assert len(list_eval_logs(str(workspace.logs))) == 2


def test_scanning_rides_the_workers_and_a_relaunch_attaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Online scanning, closed once for real: the bracket the launch lays down and the rows the workers record are about the same directory.

    The layers are otherwise tested against fixtures of each other's shape — the bracket against hand-written `_scan.json`s (`tests/scan/test_bracket.py`), the worker against injected specs (upstream's selection suite) — and what only a real fleet shows is that they agree: the spec the launch writes is the one two concurrent workers find, the definition's scanner and both injected ones record one buffer row per transcript, and a re-launch attaches to the directory rather than resetting it.

    **Budget: two launches** — one capture and two workers, then a re-launch costing a second capture and no workers.
    """
    import json

    from inspect_scout import Summary
    from inspect_scout._recorder.buffer import RecorderBuffer
    from inspect_steward._scan import finalize_scan, rebuild_summary

    fake_cron(monkeypatch)
    clear_credentials(monkeypatch)
    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    definition = workspace.root / "evalset.py"
    shutil.copy(FIXTURES / "scanning_evalset.py", definition)

    started = launch(workspace, definition)
    assert isinstance(started, Launch), f"refused by {started}"
    assert started.turn is not None
    assert len(started.turn.spawned) == 2

    # the bracket precedes the fleet: the merged spec — the definition's own
    # scanner beside Steward's built-in — was on disk before any worker started
    assert started.scan_dir is not None
    scan_dir = Path(started.scan_dir)
    spec = json.loads((scan_dir / "_scan.json").read_text())
    assert set(spec["scanners"]) == {"transcript_echo", "scoring_integrity"}
    committed = started.manifest.scan
    assert committed is not None and committed.injected is not None
    assert set(committed.injected) == {"scoring_integrity"}

    settle(workspace)

    # nothing has folded yet, so the compacted rows do not exist at all — the
    # state that makes the tend's fold load-bearing rather than a convenience
    assert not (scan_dir / "transcript_echo.parquet").exists()

    finished = turn(workspace)
    assert finished.summary.states["complete"] == 2

    # record-only workers: one buffer row per transcript per scanner (2
    # addition samples + 1 echo sample × 2 epochs), from two concurrent
    # writers. The buffer still holds them after the tend's fold, because that
    # fold is `complete=False` — a sibling worker's `is_recorded` must keep
    # answering, and the prune waits for signoff
    def stems(scanner: str) -> set[str]:
        sdir = RecorderBuffer.buffer_dir(str(scan_dir)) / f"scanner={scanner}"
        return {p.stem for p in sdir.glob("*.parquet")} if sdir.exists() else set()

    echoed = stems("transcript_echo")
    assert len(echoed) == 4
    # the built-in dispatched for every transcript too, reviewing with the
    # ambient model under evaluation (mockllm) — no scan model is configured
    assert stems("scoring_integrity") == echoed

    # and the tend folded them forward on the turn that reaped the workers,
    # which is what makes a landed task's findings readable within a tend of
    # its samples settling
    assert (scan_dir / "transcript_echo.parquet").exists()
    assert rebuild_summary(str(scan_dir)).scanners["transcript_echo"].scans == 4

    # coverage off the rows that fold just produced, and the one assertion here
    # that needs a real two-scanner directory: a transcript counts as recorded
    # only once *every* scanner has answered for it, which a synthesized
    # single-scanner fixture cannot establish. Four transcripts across two
    # tasks — addition's two samples and echo's one sample over two epochs
    assert finished.coverage.landed == 4
    assert finished.coverage.gap == 0
    assert all(entry.complete for entry in finished.coverage.by_task.values())

    # a re-launch attaches: same directory, same spec, rows undisturbed
    relaunched = launch(workspace, definition)
    assert isinstance(relaunched, Launch)
    assert relaunched.committed is True
    assert relaunched.scan_dir == started.scan_dir
    assert set(json.loads((scan_dir / "_scan.json").read_text())["scanners"]) == {
        "transcript_echo",
        "scoring_integrity",
    }
    assert stems("transcript_echo") == echoed

    # the terminal act: fold, prune, and a summary derived from the rows.
    # This is where the buffer's accumulated `_summary.json` would lie —
    # each of the two workers persisted only its own counts, last writer
    # winning — and the rebuild replaces it with what the compacted rows say
    summary = finalize_scan(
        log_dir=str(workspace.logs),
        scan_id=json.loads((scan_dir / "_scan.json").read_text())["scan_id"],
    )
    assert set(summary.scanners) == {"transcript_echo", "scoring_integrity"}
    assert all(scanner.scans == 4 for scanner in summary.scanners.values())
    echo_summary = summary.scanners["transcript_echo"]
    assert (echo_summary.results, echo_summary.errors) == (4, 0)
    # and the file beside the results says exactly what was returned
    on_disk = json.loads((scan_dir / "_summary.json").read_text())
    assert Summary.model_validate(on_disk) == summary
