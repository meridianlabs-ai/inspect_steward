"""Stopping a worker whose task left the definition, on a real worker.

The one claim in step 16 that no synthesized state can make. `stop_workers`
prefers `inspect ctl task cancel` over a signal specifically so that the samples
a worker has already finished *land* — and whether they land is a property of
inspect's cancel path, of the control socket being reachable while a sample is
held, and of the process exiting afterwards. All three are on the other side of
a process boundary.

**Why a launch has to stop anything at all.** `reconcile` deliberately leaves an
orphan's logs alone while something is still running it, which is right for a
tend and useless here: the worker would go on writing into `logs/` for hours
against a definition nothing agrees with, and its results would be archived only
whenever it happened to finish. So the commit that orphaned it is the thing that
has to stop it.

**Budget: two launches** — one real capture and one real worker, held at
`faulty_evalset.py`'s `run` marker. The second launch's capture is faked
(`_fake.py`), because what it needs to produce is *a manifest that no longer
names the running task*, and reaching that with a real capture would mean
editing a definition mid-run to change one identifier — a second thing to get
right, in the test whose subject is the stopping.

The hold is what makes this deterministic: the worker announces arriving inside
its solver and waits on a marker, so there is no sleep and no estimate of how
long an eval takes to get going (testing.md §4).
"""

from pathlib import Path

import pytest
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_steward._launch import Change, Launch, launch
from inspect_steward._worker import Stopped, resolve_inflight
from inspect_steward._workspace import Workspace, create_workspace

from .._fault import FAULT_FIXTURE, arm
from .._logs import SynthTask, synth_manifest, write_log
from ..schedule.test_tend import settle, turn
from ..timer._fake import clear_credentials, fake_cron
from ._fake import fake_capture

FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"

PLACEHOLDER = SynthTask("placeholder", samples=1)
"""What the second manifest asks for instead.

Complete before the launch that names it, so the tend at the end of that launch
has nothing to spawn: the test is about one worker, and a second one starting
would be noise held at the same marker.
"""


def test_a_worker_whose_task_left_the_definition_is_cancelled_and_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cron(monkeypatch)
    clear_credentials(monkeypatch)
    fault = arm(monkeypatch, tmp_path, "run:hang")

    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    definition = workspace.root / FAULT_FIXTURE
    definition.write_bytes((FIXTURES / FAULT_FIXTURE).read_bytes())

    started = launch(workspace, definition)
    assert isinstance(started, Launch), f"refused by {started}"
    assert started.turn is not None and len(started.turn.spawned) == 1
    fault.reached()

    running = resolve_inflight(workspace.inflight, workspace.workers).running
    assert len(running) == 1, "the held worker should be the only one alive"
    held = running[0]

    # the definition now asks for something else entirely, so the task this
    # worker is running is an orphan the moment the manifest is committed
    write_log(workspace.logs, PLACEHOLDER)
    fake_capture(monkeypatch, synth_manifest([PLACEHOLDER]))

    amended = launch(workspace, definition, accept_archive=True)

    assert isinstance(amended, Launch)
    assert amended.committed is True
    assert [row.identifier for row in amended.delta.of(Change.REMOVED)] == [
        held.identifier
    ]

    # asked, not killed: cancelling is what lets the partial result land, and a
    # signal is the fallback for a worker with nobody to ask
    (stop,) = amended.stopped
    assert stop.worker == held.worker
    assert stop.outcome is Stopped.CANCELLED, stop.detail
    assert stop.graceful is True

    # the launch did not wait for the exit — an unbounded flush inside a claim
    # is the one thing the claim's whole design forbids — so the log lands and
    # is archived by a later turn, exactly as any other exit is observed
    settle(workspace)
    archived = turn(workspace)

    assert archived.reaped == [held.worker]
    assert len(archived.archived) == 1
    # logs/ is the current definition's results and nothing else, which is the
    # precise meaning the archive exists to give it
    remaining = [Path(log.name).name for log in list_eval_logs(str(workspace.logs))]
    assert len(remaining) == 1 and "placeholder" in remaining[0], remaining

    # and what was archived is a real log of a real eval, which is the whole
    # difference between cancelling a worker and killing one
    landed = read_eval_log(archived.archived[0], header_only=True)
    assert landed.eval.task == "faulted"

    # **`error`, not `cancelled`** — asserted because it is surprising and
    # because something depends on it. Inspect finalizes a cancelled task's log
    # with a `TerminateTaskError`, which `observe` reads as
    # `IncompleteReason.ERROR` and `reconcile` therefore treats as a task worth
    # trying again. Harmless here, where the task has left the manifest and is
    # archived rather than respawned; not harmless for `steward stop` or the
    # smoke cap, which stop workers whose tasks desired state still names. See
    # `_worker/stop.py`. If upstream ever lands `cancelled` instead, this line
    # is where that shows up rather than in a later verb's behaviour.
    assert landed.status == "error"
    assert landed.error is not None and "cancelled" in landed.error.message
