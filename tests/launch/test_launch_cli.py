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
from typing import Any

import pytest
from click.testing import CliRunner, Result
from inspect_steward._cli.launch import delta_lines
from inspect_steward._cli.main import steward
from inspect_steward._cli.options import PASSTHROUGH
from inspect_steward._evalset.manifest import Manifest, read_manifest, write_manifest
from inspect_steward._launch import Delta, Reshaped
from inspect_steward._launch.launch import _pool_advice
from inspect_steward._workspace import (
    ALIASED,
    LAUNCHED,
    Claim,
    Workspace,
    acquire,
    create_workspace,
    read_armed,
    read_journal,
    read_launched,
    spellings,
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


def test_the_turn_settings_typed_at_launch_reach_the_launch(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """A flag that parses and is then dropped is worse than no flag.

    `launch` grew `--stall-after`, `--samples-ramp` and `--tend-interval` with
    the rule that every setting is sayable on every command that can act on it,
    and for one commit it accepted all three and passed none of them on. The
    interval is the one with a visible effect this early — the other two shape a
    turn `tests/schedule` covers — so it is what this asserts.
    """
    result = run(
        "--tend-interval", "30m", "--stall-after", "2", "--samples-ramp", "false"
    )

    assert result.exit_code == 0, result.output
    armed = read_armed(read_journal(workspace.journal).events)
    assert armed is not None and armed.interval == 1800


def test_inspects_words_reach_the_capture_and_the_manifest(
    workspace: Workspace, capture: FakeCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch flag, a scoped alias, and inspect's own variable, narrowest first.

    Reaching the *capture* is the assertion that matters. `epochs` and `limit`
    decide how many samples a task has, so an enumeration made without them
    describes a run that is not happening — and every convergence check Steward
    performs is `samples × epochs` against a landed log.
    """
    monkeypatch.setenv("INSPECT_EVAL_EPOCHS", "2")
    monkeypatch.setenv("STEWARD_EPOCHS", "3")
    monkeypatch.setenv("INSPECT_EVAL_MAX_SANDBOXES", "6")

    assert run("--no-timer", "--limit", "5").exit_code == 0

    (call,) = capture.calls
    assert call.overrides is not None
    # the flag, then the alias over inspect's own, then inspect's own alone
    assert call.overrides.limit == 5
    assert call.overrides.epochs == 3
    assert call.overrides.max_sandboxes == 6
    # and the manifest is where every later tend reads them, since neither a
    # flag nor a shell export survives to the 02:00 turn
    assert committed(workspace).overrides == call.overrides


def test_a_run_that_overrides_nothing_records_nothing(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Silence is not an empty document — the definition's own values stand."""
    assert run("--no-timer").exit_code == 0

    (call,) = capture.calls
    assert call.overrides is None
    assert committed(workspace).overrides is None


def test_a_meaningless_override_is_refused_at_the_door(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """The same refusal the variable earns, because it is the same parser."""
    result = run("--no-timer", "--epochs", "yes")

    assert result.exit_code == 2
    assert capture.calls == []


def test_the_log_directory_is_not_inspects_to_move(
    workspace: Workspace, capture: FakeCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`INSPECT_LOG_DIR` is refused rather than honoured or ignored.

    Honouring it would put a worker's logs where no tend reads, so every task
    would land and then read as never started; ignoring it would do the right
    thing while telling the operator nothing about why their variable did not
    take. Refused before the capture, like the credentials check.
    """
    monkeypatch.setenv("INSPECT_LOG_DIR", "s3://somewhere/else")

    result = run("--no-timer")

    assert result.exit_code == 1
    assert "INSPECT_LOG_DIR" in result.output
    assert capture.calls == []


def test_the_passthrough_flags_are_generated_and_belong_to_launch_alone() -> None:
    """Every overridable argument gets a flag naming every variable it answers to, and only the command that shapes a run has them.

    Generated rather than written out, so a field added upstream appears here
    without anybody noticing it had to — which is also why this asserts the
    whole set rather than a sample of it.

    The variables are asserted too, because a spelling that works and is not
    documented is indistinguishable from one that does not work — and they are
    taken from `spellings`, so this checks that the help says what upstream
    actually reads rather than what a list here claims.
    """
    launch_help = CliRunner().invoke(steward, ["launch", "--help"]).output
    tend_help = CliRunner().invoke(steward, ["tend", "--help"]).output

    assert "--log-dir" not in launch_help
    for field in ALIASED:
        flag = f"--{field.replace('_', '-')}"
        assert flag in launch_help, field
        assert flag not in tend_help, field
        for name in spellings(field):
            assert name in launch_help, name
    # under their own heading, because thirty-seven of them would otherwise bury
    # the six a person types
    assert PASSTHROUGH in launch_help


def test_the_startup_memory_bound_is_printed_once(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """A launch echoes a turn like every other verb, so it must not say this itself.

    Found by running the M2 gate rather than by a test: `launch` printed the
    line and then echoed a turn that printed it again, identically, because
    both derive the width from the same manifest task count. Pinned as a count
    rather than as a presence, since presence is what was already true.
    """
    measured = capture.manifest.model_copy(
        update={
            "source": capture.manifest.source.model_copy(
                update={"capture_rss": 1 << 30}
            )
        }
    )
    capture.manifest = measured
    write_manifest(measured, workspace.manifest)

    result = run()

    assert result.exit_code == 0, result.output
    assert result.output.count("startup memory:") == 1


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


def test_the_refused_preview_names_the_fleet_a_reshape_would_stop() -> None:
    """The preview is what the operator decides `--accept-archive` with.

    A reshape is reported rather than gated, but it arrives beside changes that
    *are* gated — and stopping every worker in the fleet is a cost of accepting
    them together. A relocation has always said how many it stops; reshaping
    stops them for the same reason and said nothing, so the one sentence the
    reader gets was missing the larger half of what it would do.
    """
    lines = delta_lines(
        Delta(reshaped=Reshaped(fields=("limit",), affected=2, workers=("a_1", "b_1")))
    )

    assert any("the samples change: limit" in line for line in lines)
    assert any("2 tasks with results would run again" in line for line in lines)
    assert any(
        "2 workers running the old one would be stopped" in line for line in lines
    )


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


def test_a_launch_lays_the_scan_directory_down_and_writes_the_scanners_down(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Scanning rides every run: the built-in scanner is injected unconditionally, the scan directory exists before the first turn could spawn a worker into it, and both the manifest and the journal say so.

    The manifest carrying `injected` is what lets a scheduled tend build every worker's selection; the journal naming the set is what lets `status` answer *what scans here*.
    """
    result = run("--no-timer")
    assert result.exit_code == 0

    manifest = committed(workspace)
    assert manifest.scan is not None
    assert manifest.scan.injected is not None
    assert set(manifest.scan.injected) == {"scoring_integrity"}

    scan_id = (workspace.logs / ".eval-set-id").read_text().strip()
    scan_json = workspace.logs / "scans" / f"scan_id={scan_id}" / "_scan.json"
    spec = json.loads(scan_json.read_text())
    assert set(spec["scanners"]) == {"scoring_integrity"}
    assert spec["scan_id"] == scan_id

    assert launched(workspace)[-1]["scanners"] == ["scoring_integrity"]


def test_an_operator_scanner_colliding_with_the_builtin_refuses_before_anything_is_written(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """A collision cannot be resolved without silently changing what one of the two records, so it is refused at the launch — the one moment a human is present to rename it."""
    workspace.directives.write_text(
        "scanners:\n  scoring_integrity:\n    name: some_pkg/other\n"
    )

    result = run("--no-timer")

    assert result.exit_code == 1
    assert "scoring_integrity" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert launched(workspace) == []


def test_a_relaunch_admits_added_scanners_and_refuses_changed_ones(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """The scan directory is the witness: an added scanner records from here forward, a changed one would silently change what its recorded rows mean and refuses.

    Keyed off the directory beside the logs rather than `.steward/`, which the design tells people they may delete.
    """
    assert run("--no-timer").exit_code == 0

    # an added scanner is admitted, and the merged spec on disk grows
    workspace.directives.write_text("scanners:\n  mine:\n    name: some_pkg/mine\n")
    assert run("--no-timer").exit_code == 0
    scan_id = (workspace.logs / ".eval-set-id").read_text().strip()
    scan_json = workspace.logs / "scans" / f"scan_id={scan_id}" / "_scan.json"
    assert set(json.loads(scan_json.read_text())["scanners"]) == {
        "scoring_integrity",
        "mine",
    }

    # the same name with different substance is a different scanner
    workspace.directives.write_text(
        "scanners:\n  mine:\n    name: some_pkg/mine\n    params:\n      x: 1\n"
    )
    result = run("--no-timer")
    assert result.exit_code == 1
    assert "mine" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)

    # and dropping it again is a removal, refused on the same grounds
    workspace.directives.write_text("")
    result = run("--no-timer")
    assert result.exit_code == 1
    assert "mine" in result.output


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


def test_a_log_store_is_recorded_and_an_empty_one_is_refused(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """All that can be checked about a store before anything reads one.

    Recorded rather than acted on: publication arrives with step 33, and a path
    is not resolved because a store is created when something first publishes to
    it rather than when a launch mentions it.
    """
    # a usage error at the door rather than a refusal inside the command: an
    # empty value cannot mean anything, and YAML would read it as no preference
    assert run("--no-timer", "--log-store", "   ").exit_code == 2

    assert run("--no-timer", "--log-store", "s3://bucket/store").exit_code == 0
    (event,) = launched(workspace)
    assert event["log_store"] == "s3://bucket/store"


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


def test_a_launch_records_the_directory_it_resolved(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Where the logs go is decided once, by the launch, and carried by the manifest.

    Deriving it per turn would mean re-reading an environment a scheduler does
    not supply — so this is the field every later tend reads instead.
    """
    assert run("--no-timer").exit_code == 0

    assert committed(workspace).log_dir == str(workspace.logs)


def test_a_root_gives_the_run_a_directory_of_its_own_under_it(
    workspace: Workspace, capture: FakeCapture, tmp_path: Path
) -> None:
    """`log_root` answers the silence, and the answer names this workspace.

    Two workspaces sharing one directory would overwrite each other's
    `status.md` and `journal.jsonl`, since each propagates into its own log
    directory — so the root is a parent, never the directory itself.
    """
    root = tmp_path / "runs"

    # a relocation, because the committed run's results are in the workspace
    result = run("--no-timer", "--log-root", str(root), "--accept-archive")

    assert result.exit_code == 0, result.output
    assert committed(workspace).log_dir == str(root / workspace.root.name)


def test_a_definition_that_names_a_log_dir_is_left_alone_by_a_root(
    workspace: Workspace, capture: FakeCapture, tmp_path: Path
) -> None:
    # the root supplies a default; it never rebases a stated answer. Steward
    # refuses every *override* of a definition's log_dir, and this is not one
    capture.manifest = synth_manifest([ADDITION, ECHO], log_dir="logs")

    result = run("--no-timer", "--log-root", str(tmp_path / "runs"))

    assert result.exit_code == 0, result.output
    assert committed(workspace).log_dir == str(workspace.logs)


def test_changing_the_root_is_a_relocation_rather_than_a_silent_move(
    workspace: Workspace, capture: FakeCapture, tmp_path: Path
) -> None:
    """The reason the resolved directory is recorded rather than recomputed.

    A root arrives in the environment, so it can differ between two launches
    without anything in the workspace changing. Every identifier survives the
    move, so the delta's rows are empty — and comparing the new directory
    against one *re-derived from the same new root* would compare a value with
    itself, pass the gate, and strand a directory full of results.
    """
    root = tmp_path / "runs"
    assert run("--no-timer", "--log-root", str(root), "--accept-archive").exit_code == 0
    write_log(root / workspace.root.name, ADDITION)
    write_log(root / workspace.root.name, ECHO)

    # the same root again is not a move
    assert run("--no-timer", "--log-root", str(root)).exit_code == 0

    # dropping it is
    refused = run("--no-timer")

    assert refused.exit_code == 1
    assert "the log directory moves" in refused.output
    assert committed(workspace).log_dir == str(root / workspace.root.name)


def test_asking_for_a_root_and_for_none_is_a_usage_error(
    workspace: Workspace, capture: FakeCapture
) -> None:
    result = run("--log-root", "/data/runs", "--no-log-root")

    assert result.exit_code == 2
    assert "Pass whichever you meant" in result.output


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


def test_the_log_store_is_said_three_ways_and_the_narrowest_wins(
    workspace: Workspace, capture: FakeCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setting resolves like every other one Steward owns.

    The file is what this project reuses from, the variable is what this machine
    has, and the flag is this launch — which is the whole reason the file gained
    a key that execution.md once routed to the environment alone.
    """
    workspace.directives.write_text("log_store: s3://project/store\n")
    assert run("--no-timer").exit_code == 0
    assert launched(workspace)[-1]["log_store"] == "s3://project/store"

    monkeypatch.setenv("STEWARD_LOG_STORE", "s3://team/store")
    assert run("--no-timer").exit_code == 0
    assert launched(workspace)[-1]["log_store"] == "s3://team/store"

    assert run("--no-timer", "--log-store", "s3://mine/store").exit_code == 0
    assert launched(workspace)[-1]["log_store"] == "s3://mine/store"


def test_a_launch_can_decline_the_store_the_workspace_configured(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """`false` is what replaced `none`, and declining resolves the same as never having one.

    The two are indistinguishable in effect — both run against no store — so
    nothing records the difference.
    """
    workspace.directives.write_text("log_store: s3://project/store\n")

    assert run("--no-timer", "--no-log-store").exit_code == 0
    assert launched(workspace)[-1]["log_store"] is None

    # and the file can decline what the machine configured, which is the same
    # act one spelling out
    workspace.directives.write_text("log_store: false\n")
    assert run("--no-timer").exit_code == 0
    assert launched(workspace)[-1]["log_store"] is None


def test_asking_for_a_store_and_for_none_is_a_usage_error(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Two flags saying opposite things is a typo, not a precedence question."""
    result = run("--no-timer", "--log-store", "s3://bucket/store", "--no-log-store")

    assert result.exit_code == 2
    assert "whichever you meant" in result.output


def test_the_retired_none_is_refused_by_name(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """`none` used to mean *no store* and would now mean a directory called none.

    Taking it literally is the one reading nobody intends, so it is refused
    pointing at `false` rather than silently obeyed.
    """
    result = run("--no-timer", "--log-store", "none")

    assert result.exit_code == 2
    assert "`false` now" in result.output


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


def test_a_relaunch_reuses_the_committed_overrides(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """The amend path must not un-shape the run it is amending.

    `steward launch` typed a second time to pick up an edited definition would
    otherwise recapture at the definition's own epochs and limit, and every log
    the run has produced would read as `reshaped` at the next tend and run
    again. That loss arrives through a gate `--accept-archive` does not guard,
    because nothing leaves `logs/`.
    """
    assert run("--no-timer", "--epochs", "3").exit_code == 0
    assert capture.calls[-1].overrides is not None
    capture.manifest = committed(workspace)

    assert run("--no-timer").exit_code == 0
    remembered = capture.calls[-1].overrides
    assert remembered is not None and remembered.epochs == 3

    # and one flag replaces the whole set rather than merging into it, the same
    # wholesale replacement `-A` performs -- a per-field merge would make *this
    # run has no epochs* unsayable
    assert run("--no-timer", "--limit", "5").exit_code == 0
    replaced = capture.calls[-1].overrides
    assert replaced is not None and replaced.limit == 5 and replaced.epochs is None


def test_overrides_can_be_cleared_back_to_the_definitions_own_shape(
    workspace: Workspace, capture: FakeCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-overrides` is `--no-args` for inspect's vocabulary, and displaces the environment too.

    Without it, a run once launched with `--epochs 3` could never be launched
    without it again: silence reuses and any flag sets something.
    """
    assert run("--no-timer", "--epochs", "3").exit_code == 0
    capture.manifest = committed(workspace)

    monkeypatch.setenv("INSPECT_EVAL_MAX_SAMPLES", "20")
    assert run("--no-timer", "--no-overrides").exit_code == 0

    assert capture.calls[-1].overrides is None
    assert committed(workspace).overrides is None


def test_asking_for_no_overrides_and_for_one_at_once_is_a_usage_error(
    workspace: Workspace, capture: FakeCapture
) -> None:
    result = run("--no-timer", "--no-overrides", "--epochs", "2")

    assert result.exit_code != 0
    assert "--no-overrides" in result.output
    assert capture.calls == []


def test_asking_for_no_arguments_and_for_arguments_at_once_is_a_usage_error(
    workspace: Workspace, capture: FakeCapture
) -> None:
    """Two flags that contradict each other, caught before the definition runs."""
    result = run("--no-timer", "--no-args", "-A", "scale=2")

    assert result.exit_code != 0
    assert "--no-args" in result.output
    assert capture.calls == []


# --- docker's address pools, offered once ---------------------------------


@pytest.fixture
def cramped_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon on the built-in pools, under a run that wants more than they hold."""
    import importlib

    from inspect_steward._launch.pools import PoolAdvice

    module = importlib.import_module("inspect_steward._launch.launch")

    def cramped(wanted: int, timeout: float = 0.0) -> PoolAdvice:
        return PoolAdvice(networks=32, wanted=128, config=Path("/nowhere/daemon.json"))

    monkeypatch.setattr(module, "advise", cramped)


def test_a_cramped_daemon_is_named_with_the_json_that_fixes_it(
    workspace: Workspace, capture: FakeCapture, cramped_docker: None
) -> None:
    """Declining is the default, and *no* means *not by you* rather than *never*.

    A person is being asked to let a tool edit their Docker configuration, so
    what it would write is printed before the question rather than after the
    yes -- and printed again when they say no, with the file to put it in.
    """
    result = run("--no-timer")

    assert result.exit_code == 0, result.output
    assert "docker will run out of networks" in result.output
    assert "128 concurrent sandboxes" in result.output
    # the fix itself, quotable straight out of the terminal
    assert '"default-address-pools"' in result.output
    assert '"size": 20' in result.output
    assert "/nowhere/daemon.json" in result.output
    # the restart is named and never performed -- it stops every container on
    # this machine, which on a shared box is somebody else's work. Compared
    # against the function rather than a literal, because the command differs
    # per platform and this file runs on all of them
    from inspect_steward._launch.pools import restart_command

    assert restart_command() in result.output


def test_the_pools_are_offered_once_per_workspace(
    workspace: Workspace, capture: FakeCapture, cramped_docker: None
) -> None:
    """The condition belongs to the host, so hearing it twice teaches nothing.

    Somebody who has heard it and left their daemon alone has answered for
    every later launch too, and advice repeated on each one is what trains a
    reader to scroll past the output.
    """
    first = run("--no-timer")
    second = run("--no-timer")

    assert "docker will run out of networks" in first.output
    assert "docker will run out of networks" not in second.output


def test_the_advice_reaches_json_for_a_caller_with_no_terminal(
    workspace: Workspace, capture: FakeCapture, cramped_docker: None
) -> None:
    result = run("--no-timer", "--json")

    assert result.exit_code == 0, result.output
    pools = json.loads(result.output)["pools"]
    assert (pools["networks"], pools["wanted"]) == (32, 128)


def test_a_roomy_daemon_says_nothing(
    workspace: Workspace, capture: FakeCapture
) -> None:
    # the autouse `no_ambient_docker` guard stands in for a daemon with room:
    # the launch output must carry no advice at all
    result = run("--no-timer")

    assert result.exit_code == 0, result.output
    assert "address-pools" not in result.output


def test_a_journal_that_will_not_read_costs_the_advice_and_not_the_launch(
    workspace: Workspace, capture: FakeCapture, cramped_docker: None
) -> None:
    """The opposite of what the smoke gate does with the same failure, deliberately.

    That one is a warning about the run, where saying nothing asserts something
    false. This is a tip about somebody's Docker daemon, where saying nothing
    asserts nothing — and what an unreadable journal actually costs here is the
    *once*, so repeating a tip they already declined is the failure to avoid.
    What must not happen either way is a traceback: this runs after the run is
    committed, and a launch that succeeded and then died rendering an aside is
    a launch nobody can tell succeeded.

    Called directly rather than driven through the CLI, because a journal this
    unreadable takes the launch down two steps earlier at the `launched` append
    — which is correct there and stated as such: an event that did not land is
    a hole in the only copy. What is reachable is the narrower case this covers,
    a journal that would not read when this asked.
    """
    workspace.journal.write_text("", encoding="utf-8")
    workspace.journal.chmod(0o000)
    try:
        advice = _pool_advice(workspace, capture.manifest)
    finally:
        workspace.journal.chmod(0o644)

    assert advice is None


def test_advice_that_could_not_be_journalled_is_still_given(
    workspace: Workspace,
    capture: FakeCapture,
    cramped_docker: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the other half, and it fails the other way: the record is what makes this
    # once-only, and the advice is useful without it. Said now, and said again
    # next launch, which is noise rather than a wrong answer
    import importlib

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only file system")

    # by name, because `_launch` re-exports the `launch` *function* under the
    # module's own path and the attribute lookup finds that instead
    module = importlib.import_module("inspect_steward._launch.launch")
    monkeypatch.setattr(module, "append_event", refuse)

    advice = _pool_advice(workspace, capture.manifest)

    assert advice is not None and advice.networks == 32
