"""`steward launch`, as a shell meets it.

Thin where the layers below are covered: the delta's rows are settled in
`test_delta.py` and the turn in `tests/schedule/`, so what is only true here is
the shell contract — the gate refuses with a non-zero exit and a delta above it,
a refusal is a message rather than a traceback, the timer is armed by default,
and the things a launch writes down are written down.

**Two seams are faked and everything else is real**: the capture, which is a
subprocess (`_fake.py`), and the crontab, which is a file on the machine running
the suite (`tests/timer/_fake.py`). The manifests come from `_logs.py`, so the
identifiers a synthesized log carries are the ones the fake capture claims —
which is what lets these cases exercise the real archive, the real journal, and
the real first turn without an eval.

**No launches, and no workers**: every case is a run whose tasks already have
logs, so the tend that ends a launch finds nothing to spawn.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from inspect_steward._cli.main import steward
from inspect_steward._evalset.manifest import Manifest, read_manifest, write_manifest
from inspect_steward._workspace import (
    LAUNCHED,
    Claim,
    Workspace,
    acquire,
    create_workspace,
    read_armed,
    read_journal,
    read_launched,
)

from .._logs import DEFINITION, SynthTask, synth_manifest, write_log
from ..schedule.test_tend import prepared
from ..timer._fake import FakeCrontab, clear_credentials, fake_cron
from ._fake import FakeCapture, fake_capture

ADDITION = SynthTask("addition", samples=4)
ECHO = SynthTask("echo", samples=2)
SCALED = SynthTask("addition", args={"scale": 2}, samples=4)
"""The same task with an edited argument: a new identifier under the same name and model, which is the shape the gate exists for."""


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm from a shell holding nothing worth losing (`_fake`)."""
    clear_credentials(monkeypatch)


@pytest.fixture(autouse=True)
def crontab(monkeypatch: pytest.MonkeyPatch) -> FakeCrontab:
    """Every scheduler's system, faked at the one seam all three go through.

    `launch` has no `--scheduler` flag, deliberately (plan.md §9), so which
    backend these cases arm is a property of the machine running them: launchd
    on macOS, systemd on most Linux, cron on a container with neither. All
    three are safe here — every one of them reaches the system through
    `run_command`, which this replaces, and the two that also write a file
    (a plist, a pair of units) write it under `Path.home()`, which the autouse
    `isolated_user_data` fixture has already moved out of the real home.

    So no case below names a backend. What `launch` promises is that a timer is
    armed or the launch fails; *which* one is `tests/timer/test_detect.py`'s.
    """
    return fake_cron(monkeypatch)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A launched run that has already converged, entered as a shell enters it.

    Rooted at `tmp_path` rather than a child of it, because the conftest reaper
    sweeps `tmp_path/.steward/workers` — a test that did spawn something would
    otherwise leak it.
    """
    create_workspace(tmp_path, git=False)
    workspace, _ = prepared(tmp_path, [ADDITION, ECHO])
    for task in (ADDITION, ECHO):
        write_log(workspace.logs, task)
    monkeypatch.chdir(workspace.root)
    return workspace


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch, workspace: Workspace) -> FakeCapture:
    """A capture that agrees with what is already committed.

    The starting point for every case: a launch against it changes nothing, so a
    test that wants a change says which one by reassigning `manifest`.
    """
    return fake_capture(monkeypatch, committed(workspace))


def committed(workspace: Workspace) -> Manifest:
    return read_manifest(workspace.manifest)


def run(*args: str) -> Result:
    return CliRunner().invoke(steward, ["launch", *args])


def launched(workspace: Workspace) -> list[dict[str, object]]:
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == LAUNCHED
    ]


def test_a_launch_arms_a_timer_and_writes_itself_down(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """The two things a launch leaves behind that nothing else would.

    Arming is not a step in a runbook somebody remembers (execution.md §8.3),
    and the journal entry is what makes an unsupervised run detectable later.
    """
    result = run()

    assert result.exit_code == 0, result.output
    assert "armed " in result.output
    armed = read_armed(read_journal(workspace.journal).events)
    assert armed is not None and armed.interval == 600

    (event,) = launched(workspace)
    assert event["timer"] == armed.scheduler
    assert event["tasks"] == 2
    # relative to the workspace, because every later tend resolves it against
    # the root -- an absolute path breaks the first time the workspace moves
    assert event["definition"] == DEFINITION


def test_an_additive_delta_is_committed_without_being_asked(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Adding work is what the human just typed; asking whether they meant it is the interruption this design exists to remove."""
    capture.manifest = synth_manifest([ADDITION, ECHO, SynthTask("extra", samples=3)])

    result = run("--no-timer")

    assert result.exit_code == 0, result.output
    assert "add" in result.output
    assert len(committed(workspace).tasks) == 3


def test_the_archive_gate_refuses_and_accept_archive_proceeds(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """The gate, both ways, and the delta above it.

    Printing the delta on a refusal is the point rather than a nicety: a refusal
    whose reason is invisible teaches people to reach for the flag reflexively,
    which is the same as having no gate.
    """
    capture.manifest = synth_manifest([SCALED, ECHO])
    before = committed(workspace)

    refused = run("--no-timer")

    assert refused.exit_code == 1
    assert "superseded" in refused.output
    assert "--accept-archive" in refused.output
    # nothing at all: not the manifest, not the logs, not the journal
    assert committed(workspace) == before
    assert launched(workspace) == []
    assert len(list(workspace.logs.iterdir())) == 2

    accepted = run("--no-timer", "--accept-archive")

    assert accepted.exit_code == 0, accepted.output
    assert committed(workspace).tasks[0].identifier == SCALED.identifier
    assert [path.name for path in workspace.logs_archive.iterdir()] != []


def test_launching_without_a_timer_reports_the_run_as_unsupervised(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """`--no-timer` is legitimate and must not be silent.

    The `unsupervised` item is gated on a timer having *ever* been armed, so
    without the journal entry a deliberately hand-driven run looks exactly like
    a workspace nobody started (execution.md §8.3).
    """
    capture.manifest = synth_manifest([ADDITION, ECHO, SynthTask("more", samples=3)])

    result = run("--no-timer")

    assert result.exit_code == 0, result.output
    assert "no timer armed" in result.output
    (event,) = launched(workspace)
    assert event["timer"] is None
    assert read_armed(read_journal(workspace.journal).events) is None
    assert read_launched(read_journal(workspace.journal).events) is not None

    # and the next reader is told, which is the half that matters
    reported = CliRunner().invoke(steward, ["status"])
    assert "no timer is armed" in reported.output


def test_a_relaunch_reuses_the_committed_arguments_and_type(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Forgetting `-A` on a re-launch must not capture a different eval set.

    It would propose archiving everything the run has done — a data-losing
    outcome produced by omitting a flag, which is exactly the mistake the gate
    should not have to be the last line of defence against.
    """
    stored = committed(workspace)
    with_args = stored.model_copy(
        update={"source": stored.source.model_copy(update={"args": {"scale": 2}})}
    )
    write_manifest(with_args, workspace.manifest)
    capture.manifest = with_args

    assert run("--no-timer").exit_code == 0
    assert capture.calls[-1].args == {"scale": 2}
    assert capture.calls[-1].type == "evalset"

    # an explicit flag still wins over the remembered one
    assert run("--no-timer", "-A", "scale=3", "--accept-archive").exit_code == 0
    assert capture.calls[-1].args == {"scale": 3}


def test_a_definition_that_declares_a_scanner_is_refused_before_anything_is_written(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Refused here rather than by every one of its workers failing identically.

    Capture reports whether the definition scans precisely so a runner learns it
    at enumeration time.
    """
    capture.manifest = synth_manifest([ADDITION, ECHO], scanners=True)

    result = run("--no-timer")

    assert result.exit_code == 1
    assert "scanner" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert launched(workspace) == []


def test_the_credentials_check_refuses_before_the_capture(
    workspace: Workspace, capture: FakeCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A five-minute Hawk capture that ends in *put your API key in .env* is a worse version of the same message.

    That the capture never ran is the whole claim, and `calls` is what states it.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")

    refused = run()

    assert refused.exit_code == 1
    assert "OPENAI_API_KEY" in refused.output
    assert capture.calls == []

    assert run("--no-env-check").exit_code == 0
    # and `--no-timer` skips it too, there being no timer to lose it
    assert run("--no-timer").exit_code == 0


def test_the_capture_runs_in_the_workspace_even_from_a_subdirectory(
    workspace: Workspace, capture: FakeCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest records no cwd, so enumerating under one directory and executing under another is undetectable downstream.

    Workers are pinned to the workspace root, so the capture must be too — and
    the definition has to be found from wherever the shell happens to be.
    """
    subdirectory = workspace.root / "notes"
    subdirectory.mkdir()
    monkeypatch.chdir(subdirectory)

    assert run("--no-timer").exit_code == 0
    assert capture.calls[-1].cwd == workspace.root
    assert capture.calls[-1].definition == workspace.root / DEFINITION
    assert committed(workspace).source.path == DEFINITION


def test_a_held_claim_stops_the_launch_and_says_who_has_it(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Unlike a refused tend, a refused launch *is* a problem: the instruction somebody just typed did not happen."""
    held = acquire(workspace.claim, command="tend")
    assert isinstance(held, Claim)
    try:
        result = run("--no-timer", "--no-break-claim")
    finally:
        held.release()

    assert result.exit_code == 1
    assert "tend holds the claim" in result.output
    assert capture.calls == []
    assert launched(workspace) == []


def test_a_store_is_recorded_and_an_empty_one_is_refused(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """All that can be checked about a store before anything reads one.

    Recorded rather than acted on: publication arrives with step 33, and a path
    is not resolved because a store is created when something first publishes to
    it rather than when a launch mentions it.
    """
    assert run("--no-timer", "--store", "   ").exit_code == 1

    assert run("--no-timer", "--store", "s3://bucket/store").exit_code == 0
    (event,) = launched(workspace)
    assert event["store"] == "s3://bucket/store"


def test_a_workspace_with_no_definition_is_told_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message naming what to create, rather than a traceback from a missing file."""
    create_workspace(tmp_path, git=False)
    # `init` leaves a placeholder, which is the thing being removed: a
    # workspace whose definition somebody deleted, not one that never had one
    (tmp_path / DEFINITION).unlink()
    monkeypatch.chdir(tmp_path)

    result = run("--no-timer")

    assert result.exit_code == 1
    assert "no definition" in result.output


def test_a_launch_is_a_document_when_asked(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """The agent's surface: the delta and the turn as one object rather than a table to parse."""
    capture.manifest = synth_manifest([ADDITION, ECHO, SynthTask("extra", samples=3)])

    result = run("--no-timer", "--json")

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["committed"] is True
    assert len(document["delta"]["changes"]) == 1
    # the manifest is pydantic rather than a dataclass, so it needs its own dump
    # -- without one it renders as a single `str()` of the whole eval set
    assert len(document["manifest"]["tasks"]) == 3
    assert document["turn"]["executed"] is True


def test_moving_the_log_directory_is_refused_rather_than_rerunning_everything(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """The hole the archive gate alone leaves, and it costs the same thing the gate exists to protect.

    Change `log_dir` in the definition and every identifier stays the same, so
    the delta is empty and commits unasked — and the first tend then reads an
    empty directory, finds every task missing, and re-runs the whole sweep while
    the real results sit in a directory nothing looks at any more. Silent, and
    exactly as expensive as the archiving case.
    """
    capture.manifest = synth_manifest([ADDITION, ECHO], log_dir="logs2")
    before = committed(workspace)

    refused = run("--no-timer")

    assert refused.exit_code == 1
    assert "logs2" in refused.output
    assert committed(workspace) == before
    assert launched(workspace) == []
    assert not (workspace.root / "logs2").exists()

    accepted = run("--no-timer", "--accept-archive")

    assert accepted.exit_code == 0, accepted.output
    assert committed(workspace).options["log_dir"] == "logs2"


def test_no_timer_removes_a_timer_that_is_already_armed(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """`--no-timer` says the run is unsupervised, so the run has to become unsupervised.

    Skipping the arming is not enough on a re-launch: the scheduler entry from
    the previous launch keeps firing, `read_armed` keeps reporting it, and the
    operator has been told the opposite of what is true while expensive work
    goes on being scheduled against their explicit instruction.
    """
    assert run().exit_code == 0
    assert read_armed(read_journal(workspace.journal).events) is not None

    result = run("--no-timer")

    assert result.exit_code == 0, result.output
    assert read_armed(read_journal(workspace.journal).events) is None
    assert "disarmed" in result.output


def test_the_store_falls_back_to_the_environment(
    workspace: Workspace, capture: FakeCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--store` is documented as a launch override of `INSPECT_STEWARD_STORE`, which means there is something to override."""
    monkeypatch.setenv("INSPECT_STEWARD_STORE", "s3://team/store")

    assert run("--no-timer").exit_code == 0
    assert launched(workspace)[-1]["store"] == "s3://team/store"

    # and the flag wins over it, which is what makes it an override
    assert run("--no-timer", "--store", "none").exit_code == 0
    assert launched(workspace)[-1]["store"] == "none"


def test_an_exported_but_empty_store_is_unset_rather_than_a_typo(
    workspace: Workspace, capture: FakeCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same reading `_timer.env` gives an exported-but-empty credential.

    A variable someone exported and left empty carries nothing, and refusing to
    launch over it would be refusing a correct setup. A value *typed* on the
    command line is a different act, and an empty one there is a typo.
    """
    monkeypatch.setenv("INSPECT_STEWARD_STORE", "")

    assert run("--no-timer").exit_code == 0
    assert launched(workspace)[-1]["store"] is None
    assert run("--no-timer", "--store", "   ").exit_code == 1


def test_arguments_can_be_cleared_back_to_the_definitions_defaults(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Reusing the committed arguments is a safety default, and a default with no way out is a trap.

    Omitting `-A` means *the same arguments as last time*, which is what keeps a
    forgotten flag from capturing a different eval set. But it also means that
    once a launch has passed `-A`, no combination of flags asks for the
    definition's own defaults again: no `-A` reuses, and any `-A` sets something.
    `--no-args` is the way back.
    """
    stored = committed(workspace)
    with_args = stored.model_copy(
        update={"source": stored.source.model_copy(update={"args": {"scale": 2}})}
    )
    write_manifest(with_args, workspace.manifest)
    capture.manifest = with_args

    assert run("--no-timer").exit_code == 0
    assert capture.calls[-1].args == {"scale": 2}

    assert run("--no-timer", "--no-args").exit_code == 0
    # `{}` rather than `None`: the capture is being told *no arguments*, which
    # is a different instruction from *whatever you would default to*
    assert capture.calls[-1].args == {}


def test_asking_for_no_arguments_and_for_arguments_at_once_is_a_usage_error(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Two flags that contradict each other, caught before the definition runs."""
    result = run("--no-timer", "--no-args", "-A", "scale=2")

    assert result.exit_code != 0
    assert "--no-args" in result.output
    assert capture.calls == []
