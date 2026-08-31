"""What the two verbs put on a terminal, and what they promise a shell.

Thin on purpose: the turn itself is `test_tend.py`'s subject, and re-asserting
its decisions through a `CliRunner` would only test them twice. What is only
true at this layer is the shell contract — that a refusal is a normal outcome
rather than a failure, that a message meant for a person is not a traceback,
and that `--json` is a document rather than prose with braces in it.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._cli.turn import echo_turn
from inspect_steward._evalset.manifest import write_manifest
from inspect_steward._tend import Live, status_markdown
from inspect_steward._workspace import Claim, Workspace, acquire, create_workspace

from .._logs import SynthTask, write_log
from .test_tend import prepared, settle, turn


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


def test_the_memory_projection_is_silent_when_nothing_measured_it(
    workspace: Workspace,
) -> None:
    """Which is every manifest committed before the measurement existed.

    A missing figure has to read as *unknown* rather than as zero, so the line is absent rather than rendered with a nought in it.
    """
    code, output = run("status")

    assert code == 0, output
    assert "startup memory" not in output


def test_the_memory_projection_multiplies_the_measurement_by_the_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a wide run costs, at the moment somebody is choosing how wide to run.

    Two tasks and no `max_workers` is two processes, so the fleet figure is twice the per-worker one. That multiplication is the one thing this line exists to do — an operator who has just read *1.0 GiB each* still has to perform it, and on a five-hundred-task sweep that is the whole point.
    """
    create_workspace(tmp_path, git=False)
    workspace, manifest = prepared(tmp_path, [SynthTask("a"), SynthTask("b")])
    measured = manifest.model_copy(
        update={"source": manifest.source.model_copy(update={"capture_rss": 1 << 30})}
    )
    write_manifest(measured, workspace.manifest)
    monkeypatch.chdir(workspace.root)

    code, output = run("status")

    assert code == 0, output
    assert "at most 1.0 GiB per worker" in output
    assert "2.0 GiB across 2 workers" in output


def test_the_memory_projection_follows_the_width_the_operator_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Packing is the lever this line exists to make visible.

    The same two tasks in one process is one copy rather than two, which is exactly the trade `max_workers` buys — so the projection has to move when it is set, or it is describing a run nobody asked for.
    """
    create_workspace(tmp_path, git=False)
    workspace, manifest = prepared(tmp_path, [SynthTask("a"), SynthTask("b")])
    measured = manifest.model_copy(
        update={"source": manifest.source.model_copy(update={"capture_rss": 1 << 30})}
    )
    write_manifest(measured, workspace.manifest)
    monkeypatch.chdir(workspace.root)

    code, output = run("status", "--max-workers", "1")

    assert code == 0, output
    assert "1.0 GiB across 1 worker," in output


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
        pytest.param(["status", "--max-workers", "0"], id="previewed_against_none"),
    ],
)
def test_a_meaningless_ceiling_is_refused_at_the_door(
    argv: list[str], workspace: Workspace
) -> None:
    # the same refusal `_steward.yaml` makes, because this is the same setting
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


# --- ack ----------------------------------------------------------------


def broken(workspace: Workspace, name: str = "broken.eval") -> str:
    """A file that is not a log, which projects to one acknowledgeable item."""
    (workspace.logs / name).write_bytes(b"not a log")
    return f"unreadable:{name}"


def test_ack_takes_an_id(workspace: Workspace) -> None:
    identifier = broken(workspace)

    code, output = run("ack", identifier, "--reason", "known bad file")

    assert code == 0, output
    assert identifier in output
    # gone from the surface, and only the surface -- the record is the journal
    assert identifier not in run("status")[1]
    assert "known bad file" in workspace.journal.read_text(encoding="utf-8")


def test_ack_takes_an_unambiguous_prefix(workspace: Workspace) -> None:
    identifier = broken(workspace)

    code, output = run("ack", "unreadable:bro", "--reason", "fine")

    assert code == 0, output
    assert identifier in output


def test_an_ambiguous_prefix_names_the_candidates_and_writes_nothing(
    workspace: Workspace,
) -> None:
    broken(workspace, "one.eval")
    broken(workspace, "two.eval")
    before = workspace.journal.read_text(encoding="utf-8")

    code, output = run("ack", "unreadable", "--reason", "fine")

    assert code != 0
    assert "unreadable:one.eval" in output and "unreadable:two.eval" in output
    assert workspace.journal.read_text(encoding="utf-8") == before


def test_acking_the_same_thing_twice_says_who_did_it_first(
    workspace: Workspace,
) -> None:
    # the likeliest confusion by far, and an empty list cannot distinguish it
    # from a typo -- so the journal is consulted to say which one happened
    identifier = broken(workspace)
    run("ack", identifier, "--reason", "deliberate")

    code, output = run("ack", identifier, "--reason", "again")

    assert code != 0
    assert "already been acknowledged" in output
    assert "deliberate" in output


def test_an_id_that_matches_nothing_says_where_to_look(
    workspace: Workspace,
) -> None:
    code, output = run("ack", "nonsense", "--reason", "fine")

    assert code != 0
    assert "steward status" in output


def test_ack_refuses_without_a_reason(workspace: Workspace) -> None:
    # the record is the whole point of the act; one without a reason is a hole
    # in the audit trail rather than a terse entry in it
    identifier = broken(workspace)

    code, output = run("ack", identifier)

    assert code != 0
    assert "--reason" in output


def test_ack_records_who_decided(workspace: Workspace) -> None:
    # an agent disposing of something it investigated is its own decision, not
    # a person's relayed through it
    identifier = broken(workspace)

    run("ack", identifier, "--by", "agent", "--reason", "transient")

    (event,) = [
        json.loads(line)
        for line in workspace.journal.read_text(encoding="utf-8").splitlines()
        if '"acknowledged"' in line
    ]
    assert event["by"] == "agent"
    assert event["kind"] == "unreadable"


def test_ack_does_not_take_the_claim(workspace: Workspace) -> None:
    # the case that matters most: somebody reading a status while the fleet
    # converges is exactly when they decide something is fine
    identifier = broken(workspace)

    outcome = acquire(workspace.claim, command="tend")
    assert isinstance(outcome, Claim)
    with outcome:
        code, output = run("ack", identifier, "--reason", "mid-tend")

    assert code == 0, output


# --- rendering ----------------------------------------------------------


def test_status_prints_markdown_on_request_and_not_otherwise(
    workspace: Workspace,
) -> None:
    # the agent is told to relay this verbatim (agent.md, *Render the summary;
    # do not replace it*), and aligned terminal columns do not survive that
    _, markdown = run("status", "--format", "md")
    _, text = run("status")

    assert "| state | tasks |" in markdown
    assert "### tasks" in markdown
    # ...and no warning about editing a file, since this one is not a file
    assert "Regenerated every turn" not in markdown
    assert "| state | tasks |" not in text


def test_every_rendering_leads_with_the_same_verdict(workspace: Workspace) -> None:
    # settled, so all three reads see the same state: the spawned worker ran
    # the EMPTY definition and departed without a log, which anomaly detection
    # (correctly) reports — the claim here is agreement, not health
    turn(workspace)
    settle(workspace)

    _, text = run("status")
    _, markdown = run("status", "--format", "md")
    code, raw = run("status", "--json")
    document = json.loads(raw)

    glyph = document["verdict"]
    assert glyph == "⚠️"
    assert text.startswith(glyph)
    assert glyph in markdown
    assert code == 0


def test_the_live_block_replaces_the_startup_bound_and_both_renderings_agree(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under the table: what the fleet costs now, or what starting it would.

    A real turn with a synthesized fleet, because the two branches are a
    rendering decision rather than a reading one — and no synthesized *state*
    can produce a running worker.
    """
    result = turn(workspace)
    running = replace(
        result,
        progress=replace(
            result.progress, live=Live(tasks=2, refusals=3, http_retries=41)
        ),
    )

    markdown = status_markdown(running, header=False)
    echo_turn(running)
    text = capsys.readouterr().out

    for rendering in (markdown, text):
        assert "2 tasks · 3 refusals · 41 HTTP retries" in rendering
        # the caveat travels with the figures, because a total that falls as
        # tasks complete otherwise reads as a problem resolving itself
        assert "fall as tasks finish" in rendering
    # and with nothing running there is no block to caveat
    assert "refusals" not in status_markdown(result, header=False)


def test_the_tasks_table_says_what_is_still_to_run_with_nothing_running(
    workspace: Workspace,
) -> None:
    # running and connections need a worker to answer for them; what is left to
    # run does not, and gating all three together hid the queue for exactly the
    # runs that are all queue -- one waiting task, ten samples, no fleet
    markdown = status_markdown(turn(workspace), header=False)

    (row,) = [line for line in markdown.splitlines() if "`waiting`" in line]
    assert "| queued |" in markdown
    assert "| 10 |" in row
    # and the two that do need one are still absent
    assert "| running |" not in markdown and "| connections |" not in markdown


def test_markdown_says_when_a_tend_holds_the_claim(workspace: Workspace) -> None:
    # a claim is not an item -- nobody resolves it, and it is gone in seconds --
    # but it is exactly what makes an "as of" misleading, since the tend holding
    # it is about to change every number below. Both renderings have to say so
    outcome = acquire(workspace.claim, command="tend")
    assert isinstance(outcome, Claim)
    with outcome:
        _, text = run("status")
        _, markdown = run("status", "--format", "md")

    assert "holds the claim" in text
    assert "holds the claim" in markdown


def test_ack_leaves_status_md_to_the_next_tend(workspace: Workspace) -> None:
    """The one surface an ack deliberately does not touch.

    `status.md` is a tend artifact whose **age** is load-bearing: a remote
    reader detects a stopped timer, a crashed tend, or a broken sync by
    noticing it stopped changing. A writer that is not a tend would stamp it
    `as of now` and destroy exactly that signal — so the file catches up on the
    next turn, which is the same interval every other number in it is already
    stale by.
    """
    identifier = broken(workspace)
    turn(workspace)
    stamped = workspace.status.read_text(encoding="utf-8")
    assert identifier in stamped

    run("ack", identifier, "--reason", "known bad file")

    # unchanged, byte for byte -- including the `As of` it was written with
    assert workspace.status.read_text(encoding="utf-8") == stamped
    # but nothing that *computes* still reports it
    assert identifier not in run("status")[1]

    turn(workspace)
    assert identifier not in workspace.status.read_text(encoding="utf-8")
