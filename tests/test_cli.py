from pathlib import Path

import pytest
from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._evalset.manifest import write_manifest
from inspect_steward._workspace import create_workspace

from ._logs import SynthTask, synth_manifest


def test_cli_help_lists_commands() -> None:
    result = CliRunner().invoke(steward, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "launch", "runbook", "status", "tasks", "tend"):
        assert command in result.output


def test_cli_version() -> None:
    result = CliRunner().invoke(steward, ["--version"])
    assert result.exit_code == 0
    assert "steward" in result.output


# `init` itself is covered in tests/workspace/test_init_cli.py, where every
# invocation is given a directory. Invoking it here without one wrote a
# workspace into the repository -- `init` was right, the test was not.


def test_ramp_hold_and_resume_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the latch is one journal append each way, and the refusals are what make
    # the verbs safe to script: a double hold or a resume of nothing is a
    # message rather than a silent second event
    create_workspace(tmp_path, git=False)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    held = runner.invoke(steward, ["ramp", "hold", "--reason", "watch this"])
    assert held.exit_code == 0, held.output

    again = runner.invoke(steward, ["ramp", "hold", "--reason", "again"])
    assert again.exit_code != 0
    assert "already held" in again.output

    resumed = runner.invoke(steward, ["ramp", "resume"])
    assert resumed.exit_code == 0, resumed.output

    nothing = runner.invoke(steward, ["ramp", "resume"])
    assert nothing.exit_code != 0
    assert "not held" in nothing.output


def test_a_hold_on_a_task_nobody_is_running_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the hold matches on an exact identifier, so a typo would hold nothing
    # while printing that it had -- and the arm somebody had stopped trusting
    # would keep climbing on the strength of the message
    workspace = create_workspace(tmp_path, git=False).workspace
    manifest = synth_manifest([SynthTask("real")])
    write_manifest(manifest, workspace.manifest)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    typo = runner.invoke(steward, ["ramp", "hold", "raal", "--reason", "oops"])
    assert typo.exit_code != 0
    assert "no task in this run is called 'raal'" in typo.output

    named = runner.invoke(
        steward,
        ["ramp", "hold", manifest.tasks[0].identifier, "--reason", "watch this"],
    )
    assert named.exit_code == 0, named.output
