"""The loop, closed once, on a real eval — started the way a person starts one.

Everything else about the turn is settled in `test_tend.py` against synthesized
state. What no synthesized state can show is that the four layers agree with
each other about the *same* run: capture writes identifiers and a content hash,
a worker writes a log, and observation matches the two. Each layer is otherwise
tested against a fixture of the next one's shape, and a fixture is a belief
about that shape rather than the shape itself.

**Through `launch` rather than by hand.** This test used to perform launch's
four steps itself — copy the definition, capture, commit, tend — which meant the
composition a person actually runs was the one thing never exercised against a
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
    workspace.directives.write_text("---\nmax_workers: 1\n---\n", encoding="utf-8")
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
