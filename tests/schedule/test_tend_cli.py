"""What the two verbs put on a terminal, and what they promise a shell.

Thin on purpose: the turn itself is `test_tend.py`'s subject, and re-asserting
its decisions through a `CliRunner` would only test them twice. What is only
true at this layer is the shell contract — that a refusal is a normal outcome
rather than a failure, that a message meant for a person is not a traceback,
and that `--json` is a document rather than prose with braces in it.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._workspace import Claim, Workspace, acquire, create_workspace

from .._logs import SynthTask, write_log
from .test_tend import prepared, turn


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A run with one complete task, entered as a shell would enter it.

    Really initialized rather than assembled, because `Workspace.find` is part
    of what these tests are checking and it keys on the journal — which only an
    `init` writes.
    """
    create_workspace(tmp_path, git=False)
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done, SynthTask("waiting")])
    write_log(workspace.logs, done)
    monkeypatch.chdir(workspace.root)
    return workspace


def run(*argv: str) -> tuple[int, str]:
    result = CliRunner().invoke(steward, list(argv))
    return result.exit_code, result.output


def test_status_says_what_the_next_tend_would_do(workspace: Workspace) -> None:
    code, output = run("status")

    assert code == 0, output
    assert "2 tasks: 1 complete, 1 missing" in output
    assert "next tend: 1 to spawn" in output
    assert not workspace.status.exists()


def test_tend_says_what_it_did(workspace: Workspace) -> None:
    code, output = run("tend", "--max-workers", "1")

    assert code == 0, output
    assert "1 spawned" in output


def test_a_held_claim_is_an_outcome_rather_than_a_failure(
    workspace: Workspace,
) -> None:
    # a timer firing while an agent is mid-tend is the ordinary case, and exiting
    # nonzero for it would make every such minute look like a fault in a log
    outcome = acquire(workspace.claim, command="tend")
    assert isinstance(outcome, Claim)

    with outcome:
        code, output = run("tend")

    assert code == 0
    assert "nothing to do" in output


def test_json_is_a_document(workspace: Workspace) -> None:
    code, output = run("status", "--json")

    assert code == 0, output
    payload = json.loads(output)
    assert payload["executed"] is False
    assert payload["summary"]["states"]["complete"] == 1


def test_a_refusal_in_json_is_one_field_to_branch_on(workspace: Workspace) -> None:
    outcome = acquire(workspace.claim, command="tend")
    assert isinstance(outcome, Claim)

    with outcome:
        code, output = run("tend", "--json")

    assert code == 0
    assert json.loads(output)["refused"] is True


@pytest.mark.parametrize("command", ["tend", "status"])
def test_a_workspace_that_never_launched_is_told_to(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the ordinary first thing anyone types after `steward init`, so it has to
    # name the next command rather than the exception that noticed
    root = create_workspace(tmp_path / "fresh", git=False).workspace.root
    monkeypatch.chdir(root)

    code, output = run(command)

    assert code == 1
    assert "steward launch" in output
    assert "Traceback" not in output


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["tend", "--max-workers", "0"], id="a_fleet_of_none"),
        pytest.param(["tend", "--max-samples", "-1"], id="negative_concurrency"),
        pytest.param(["status", "--max-workers", "0"], id="previewed_against_none"),
    ],
)
def test_a_meaningless_ceiling_is_refused_at_the_door(
    argv: list[str], workspace: Workspace
) -> None:
    # the same refusal `_steward.md` makes, because this is the same setting
    # arriving through the other door -- and a ceiling of zero is a run that
    # tends forever and never spawns
    code, output = run(*argv)

    assert code == 2
    assert "not in the range" in output or "is not in the valid range" in output


@pytest.mark.parametrize("command", ["tend", "status"])
def test_outside_a_workspace_says_so(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    code, output = run(command)

    assert code == 1
    assert "steward init" in output


def test_tend_is_still_the_thing_status_previewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One renderer, because a wrong preview is worse than no preview.

    What `status` offers to do is what the next `tend` does, and afterwards it
    no longer offers it. Demonstrated on an archive rather than a spawn: a
    spawned worker's own liveness would decide what the second `status` says,
    and the empty definition these tests use is gone in about thirty
    milliseconds — so the assertion would be racing it.
    """
    create_workspace(tmp_path, git=False)
    done = SynthTask("done")
    ws, _ = prepared(tmp_path, [done])
    write_log(ws.logs, done)
    write_log(ws.logs, SynthTask("removed"))
    monkeypatch.chdir(ws.root)

    _, preview = run("status")
    result = turn(ws)
    _, after = run("status")

    assert "next tend: 1 to archive" in preview
    assert len(result.archived) == 1
    assert "next tend: nothing to do" in after
