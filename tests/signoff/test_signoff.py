"""`steward signoff` — the attestation, and the end of the run.

The claims worth defending: the gate names *every* reason at once and each one names the act that answers it; a hole that was ruled on is signed over and one nobody named refuses; curation moves rather than deletes, and moves the superseded attempt rather than the one the run reports; a signature is keyed to the results it covered, so a relaunch un-signs and an unlaunched edit does not; a filesystem that will not cooperate cannot unmake a decision a person made; and the timer comes down, because a signed run that goes on tending is spending money against an explicit instruction.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._evalset.archive import archive_dir
from inspect_steward._evalset.manifest import write_manifest
from inspect_steward._scan import initialize_scan, scan_dir_location
from inspect_steward._signoff import (
    EMPTY,
    FAILED,
    OPEN_WINDOW,
    STANDING,
    UNDECIDED,
    UNFINALIZED,
    UNREAD,
    UNSETTLED,
    UNSIGNED,
    Signoff,
    check,
    signoff,
)
from inspect_steward._tend import Verdict, status
from inspect_steward._tend.items import UNREADABLE
from inspect_steward._tend.turn import SCAN_FOLD_FAILED
from inspect_steward._workspace import (
    ACKNOWLEDGED,
    ACTION,
    ARMED,
    PAUSED,
    RULING,
    Workspace,
    append_event,
    create_workspace,
    read_journal,
    read_signoff,
)

from .._logs import SynthSample, SynthTask, write_log
from ..anomaly.test_items import CLASS, erroring
from ..schedule.test_tend import prepared, turn

TASK = SynthTask("probe", samples=4)
EMPTY_TASK = SynthTask("hollow", samples=0)


def done(root: Path, *tasks: SynthTask) -> Workspace:
    """A run whose every task finished cleanly, in a real workspace.

    Created through `create_workspace` rather than by writing a manifest alone,
    because the CLI finds a workspace by its journal and the verb under test is
    reached through the CLI.
    """
    create_workspace(root, git=False)
    workspace, _ = prepared(root, list(tasks) or [TASK])
    for task in tasks or (TASK,):
        write_log(workspace.logs, task)
    return workspace


def logs(workspace: Workspace) -> list[Path]:
    """The eval logs in the run's directory, past what the sync propagates into it."""
    return sorted(
        path
        for path in workspace.logs.iterdir()
        if path.suffix in (".eval", ".json") and path.name != "journal.jsonl"
    )


def ruling(workspace: Workspace, disposition: str, **fields: Any) -> None:
    payload: dict[str, Any] = {
        "class": CLASS,
        "disposition": disposition,
        "reason": "the provider was down; these are not coming back",
        "by": "kaia",
        **fields,
    }
    append_event(workspace.journal, RULING, **payload)


def sign(workspace: Workspace, **kwargs: Any) -> Signoff:
    """A signoff that is expected to run rather than be refused for the claim."""
    result = signoff(workspace, by=kwargs.pop("by", "kaia"), **kwargs)
    assert isinstance(result, Signoff), result
    return result


def kinds(result: Signoff) -> list[str]:
    return [blocker.kind for blocker in result.blockers]


def run(*argv: str) -> Any:
    return CliRunner().invoke(steward, list(argv))


# --- the gate --------------------------------------------------------------


def test_a_clean_finished_run_signs(tmp_path: Path) -> None:
    workspace = done(tmp_path)

    result = sign(workspace, note="the scores look right")

    assert result.blockers == []
    assert result.signature is not None
    assert result.signature.by == "kaia"
    assert result.signature.note == "the scores look right"
    assert result.signature.digest == turn(workspace).manifest_digest


def test_an_unfinished_run_is_refused_and_told_how_to_end_without_signing(
    tmp_path: Path,
) -> None:
    """The refusal that must not read as a quality bar.

    A project somebody abandons publishes nothing, and that is the correct
    outcome rather than a gap — so the message names the verbs that end a run
    without attesting to it, beside the one that accepts the hole by name.
    """
    workspace, _ = prepared(tmp_path, [TASK])

    result = sign(workspace)

    assert kinds(result) == [UNSETTLED]
    remedy = result.blockers[0].remedy
    assert "steward rule --disposition accept" in remedy
    assert "steward pause" in remedy
    assert result.signature is None


def test_an_open_window_is_refused_even_where_it_raised_no_item(
    tmp_path: Path,
) -> None:
    """An operator kill holds back the signature and not the invitation.

    `signoff_ready` deliberately ignores a `limit:` window — it is the
    conversation that item exists to start, and gating on it would hide the
    line that leads a person to where it gets ruled. The signature is the end
    of that conversation, so an operator kill nobody ruled on is a caveat
    missing from a record claiming to be complete.
    """
    task = SynthTask("probe", samples=4)
    workspace, _ = prepared(tmp_path, [task])
    write_log(
        workspace.logs,
        task,
        samples=[SynthSample(id="s0", epoch=1, limit="operator")],
    )
    ready = [item.kind for item in turn(workspace).items]

    result = sign(workspace)

    assert "signoff_ready" in ready, "the premise: the invitation is not held back"
    assert OPEN_WINDOW in kinds(result)


def test_every_blocker_arrives_at_once(tmp_path: Path) -> None:
    """A person who fixes one refusal and meets another has walked the loop twice."""
    workspace = erroring(tmp_path, errors=2, samples=4)
    write_log(workspace.logs, SynthTask("second", samples=4), status="started")
    prepared(tmp_path, [SynthTask("probe", samples=4), SynthTask("second", samples=4)])

    result = sign(workspace)

    assert {UNSETTLED, OPEN_WINDOW} <= set(kinds(result))


def test_an_errored_sample_no_ruling_covers_refuses(tmp_path: Path) -> None:
    workspace = erroring(tmp_path, errors=2, samples=4)

    result = sign(workspace)

    assert UNDECIDED in kinds(result)
    assert "2 errored samples" in next(
        blocker.summary for blocker in result.blockers if blocker.kind == UNDECIDED
    )


def test_a_ruled_hole_is_signed_over_and_named_in_the_signature(
    tmp_path: Path,
) -> None:
    """Accepting known holes is explicit, not blocked — the whole of §13's first property."""
    workspace = erroring(tmp_path, errors=2, samples=4)
    turn(workspace)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")

    result = sign(workspace)

    assert result.blockers == []
    assert result.signature is not None
    assert result.signature.exceptions == (CLASS,)


def test_a_log_that_will_not_read_is_refused_until_somebody_names_it(
    tmp_path: Path,
) -> None:
    """The hole nobody can size, and the ordinary shape of this gate over it.

    Every other refusal here is about a population somebody can count; this one
    is about a log whose contents are unknown, so what the numbers are over is
    unknown too. It was a warning, so a finished run containing `broken.eval`
    signed with no exceptions at all and nobody had recorded why that was
    acceptable — which is precisely the unnamed hole this gate exists to refuse.
    """
    workspace = done(tmp_path)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")

    result = sign(workspace)

    blocker = next(one for one in result.blockers if one.kind == UNREAD)
    assert "unreadable:broken.eval" in blocker.summary
    assert "steward ack" in blocker.remedy
    assert result.signature is None


def test_an_acknowledged_caveat_is_named_in_the_signature_too(tmp_path: Path) -> None:
    """One definition of a caveat, and the signature counts what it counts.

    `anomalies.md` admits a disposal that left a mark on the results exactly as
    it admits a ruling. Drawing the exceptions from the accepted windows alone
    reported "no accepted exceptions" over a run whose own caveat list named
    the log nobody could read.
    """
    workspace = done(tmp_path)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")
    turn(workspace)
    append_event(
        workspace.journal,
        ACKNOWLEDGED,
        id="unreadable:broken.eval",
        kind=UNREADABLE,
        subject="broken.eval",
        summary="could not be read",
        by="human",
        reason="a partial upload; the numbers are over the rest",
    )

    result = sign(workspace)

    assert result.signature is not None
    assert result.signature.exceptions == ("broken.eval",)
    # and the same caveat, at the two lengths the two documents carry
    assert "the numbers are over what could be read" in workspace.anomalies.read_text(
        encoding="utf-8"
    )
    assert "the numbers are over what could be read" in workspace.status.read_text(
        encoding="utf-8"
    )


def test_the_gate_is_pure_over_a_turn(tmp_path: Path) -> None:
    # every refusal is testable against state the caller assembled, which is
    # what keeps the message wording out of the filesystem's reach
    workspace = erroring(tmp_path)

    assert check(status(workspace), None)


def test_an_action_the_turn_could_not_carry_out_refuses(tmp_path: Path) -> None:
    """The gate's own inputs came from a turn that did not do what it decided.

    An acceptance is the case that makes this a refusal rather than a warning:
    the ruling settles the window and the latch subtracts the task from
    unfinished work, so a failed log amendment leaves the gate reading a
    settled run while a log on disk still says `error` and carries no
    provenance for the decision.
    """
    workspace = done(tmp_path)
    could_not = replace(
        status(workspace),
        failures=["could not accept probe@mockllm/model: read-only file system"],
    )

    (blocker,) = check(could_not, None)

    assert blocker.kind == FAILED
    assert "read-only file system" in blocker.summary
    assert "run it again" in blocker.remedy


# --- what a signature covers -----------------------------------------------


def test_a_standing_signature_refuses_a_second_one_unless_asked(
    tmp_path: Path,
) -> None:
    workspace = done(tmp_path)
    sign(workspace)

    again = sign(workspace)
    forced = sign(workspace, again=True)

    assert kinds(again) == [STANDING]
    assert "kaia signed this run" in again.blockers[0].summary
    assert forced.signature is not None


def test_a_relaunch_that_changes_the_task_set_un_signs(tmp_path: Path) -> None:
    """The attestation names what it covered, so a different task set is unsigned.

    Keyed on the manifest digest rather than a run id — derived rather than
    minted, and it gives *a signoff can be invalidated* a precise trigger
    (workflow.md §2.4).
    """
    other = SynthTask("second", samples=4)
    workspace = done(tmp_path)
    sign(workspace)
    assert turn(workspace).verdict is Verdict.SIGNED_OFF

    prepared(tmp_path, [TASK, other])
    write_log(workspace.logs, other)

    assert turn(workspace).verdict is not Verdict.SIGNED_OFF


def test_a_finding_that_arrives_after_the_signature_un_signs_it_permanently(
    tmp_path: Path,
) -> None:
    """The test is temporal, not *nothing is open now*.

    A window that opened at 3am and was ruled at 4am is closed by the time
    anybody looks, and letting the old signature come back into force over a
    finding its signer never heard of is the certification-by-default this
    machine exists to refuse. So ruling it does not restore the signature —
    signing again does.
    """
    workspace = done(tmp_path)
    sign(workspace)

    task = SynthTask("probe", samples=4)
    write_log(
        workspace.logs,
        task,
        created="2026-01-02T00:00:00Z",
        completed=2,
        samples=[SynthSample(id="s0", epoch=1, error="openai.APITimeoutError")],
    )
    turn(workspace)
    ruling(workspace, "exclude", effect="1 sample excluded from scoring")

    assert turn(workspace).verdict is not Verdict.SIGNED_OFF


def test_the_old_signature_stays_in_the_journal(tmp_path: Path) -> None:
    # a signature is a thing that happened; a second one amends rather than
    # replaces, and the record keeps what was believed at the time
    workspace = done(tmp_path)
    sign(workspace, note="first look")
    sign(workspace, by="ravi", note="second look", again=True)

    events = read_journal(workspace.journal).events
    signatures = [event for event in events if event.type == "signoff"]

    assert len(signatures) == 2
    assert signatures[0].payload["note"] == "first look"
    standing = read_signoff(events)
    assert standing is not None and standing.by == "ravi"


# --- curation --------------------------------------------------------------


def test_curation_archives_the_superseded_attempt_and_keeps_the_current_one(
    tmp_path: Path,
) -> None:
    """Nothing is deleted, and the log the run *reports* never moves."""
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK, created="2026-01-01T00:00:00Z", completed=2)
    current = write_log(workspace.logs, TASK, created="2026-01-02T00:00:00Z")

    result = sign(workspace)

    assert result.curated is not None
    assert len(result.curated.moved) == 1
    assert current.exists(), "the attempt the run reports stays where it is"
    archived = list(Path(archive_dir(str(workspace.logs))).iterdir())
    assert len(archived) == 1
    assert logs(workspace) == [current]


def test_an_accepted_short_task_keeps_its_errored_current_log(
    tmp_path: Path,
) -> None:
    """The rule is *not current*, never *the status looks bad*.

    A task latched short by an acceptance has a current log the attestation
    covers — with a caveat — so choosing on status would archive the evidence
    for the exception the signature just recorded.
    """
    workspace = erroring(tmp_path, errors=2, samples=4)
    turn(workspace)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")

    sign(workspace)

    assert len(logs(workspace)) == 1


def test_a_move_that_fails_is_reported_and_still_signs(tmp_path: Path) -> None:
    """The signature is the person's act; a filesystem must not unmake it."""
    workspace, _ = prepared(tmp_path, [TASK])
    superseded = write_log(
        workspace.logs, TASK, created="2026-01-01T00:00:00Z", completed=2
    )
    write_log(workspace.logs, TASK, created="2026-01-02T00:00:00Z")
    # a directory sitting where the archived copy would land
    archive = Path(archive_dir(str(workspace.logs)))
    archive.mkdir(parents=True)
    (archive / superseded.name).mkdir()

    result = sign(workspace)

    assert result.signature is not None
    assert result.curated is not None


def test_a_paused_run_curates_the_orphans_its_tends_left_behind(
    tmp_path: Path,
) -> None:
    """Curation takes what is left rather than assuming the other half ran.

    A tend archives every orphan the moment it meets one, which is why this
    used to skip them — but a paused run makes no changes to itself, so
    reconcile archives nothing while the pause stands, and signing a paused run
    is allowed. Between them a signed directory kept results belonging to an
    identifier the definition no longer names, with the timer down and no turn
    ever coming to tidy up.
    """
    gone = SynthTask("gone", samples=4)
    workspace = done(tmp_path)
    orphan = write_log(workspace.logs, gone)
    append_event(workspace.journal, PAUSED, by="test", reason="hold everything")
    assert len(logs(workspace)) == 2, "the premise: the pause left it there"

    result = sign(workspace)

    assert result.signature is not None
    assert result.curated is not None
    assert [entry.identifier for entry, _ in result.curated.moved] == [gone.identifier]
    assert not orphan.exists()
    assert len(logs(workspace)) == 1
    assert len(list(Path(archive_dir(str(workspace.logs))).iterdir())) == 1


def test_nothing_to_curate_writes_no_line(tmp_path: Path) -> None:
    workspace = done(tmp_path)

    result = sign(workspace)
    happened = turn(workspace).happened.entries

    assert result.curated is not None and not result.curated.moved
    assert not any("curated" in entry.text for entry in happened)


# --- ending the run --------------------------------------------------------


def test_signing_takes_the_timer_down(tmp_path: Path) -> None:
    workspace = done(tmp_path)
    append_event(workspace.journal, ARMED, scheduler="cron", interval=600, label="w")

    result = sign(workspace)

    assert result.disarmed == "cron"
    assert turn(workspace).supervision is not None
    assert turn(workspace).supervision.armed is None  # type: ignore[union-attr]


def test_a_signed_run_reads_locked_and_stops_asking_to_be_accepted(
    tmp_path: Path,
) -> None:
    workspace = done(tmp_path)
    before = turn(workspace)

    sign(workspace)
    after = turn(workspace)

    assert before.verdict is Verdict.COMPLETE
    assert after.verdict is Verdict.SIGNED_OFF
    assert after.items == []


def test_the_signature_reaches_the_files_a_reader_opens(tmp_path: Path) -> None:
    """The run never tends again, so the last turn is the one that has to be true.

    Signing after the final turn had already rendered and synced left the
    durable snapshot saying 🏁 *waiting to be accepted* forever, and a remote
    copy — the only thing a reader away from this machine has — carrying
    neither the signature nor the curation. So a second turn runs under the
    same claim once the signature is in the journal.
    """
    workspace = done(tmp_path)
    turn(workspace)
    assert "🏁" in workspace.status.read_text(encoding="utf-8")

    result = sign(workspace, note="the scores look right")

    rendered = workspace.status.read_text(encoding="utf-8")
    assert "🔒" in rendered
    assert "signed off by kaia" in rendered
    assert "the scores look right" in rendered
    # and the turn the verb reports is the one that saw the signature
    assert result.turn.verdict is Verdict.SIGNED_OFF
    assert result.warnings == []


def test_a_snapshot_that_could_not_be_written_is_said_out_loud(
    tmp_path: Path,
) -> None:
    """A signature the durable record does not carry is worth knowing about.

    The write is allowed to fail — the signature has already landed in the
    journal, and a filesystem must not unmake a decision a person made. What is
    not allowed is silence: the timer comes down straight after this, so no
    scheduled turn will ever repair the files, and the signer would be told the
    run was signed while the only thing a remote reader can see went on saying
    *finished, waiting to be accepted* forever.
    """
    workspace = done(tmp_path)
    turn(workspace)
    for path in (workspace.status, workspace.anomalies):
        path.unlink(missing_ok=True)
        path.mkdir()  # every rename onto it now fails

    result = sign(workspace)

    assert result.signature is not None, "the signature is not the write's to unmake"
    warned = "\n".join(result.warnings)
    assert "status.md could not be rewritten" in warned
    assert "anomalies.md could not be rewritten" in warned
    assert "steward tend" in warned


def test_a_signed_run_says_nothing_further(tmp_path: Path) -> None:
    """The verb sends the one terminal message; a turn afterwards sends none.

    Signing disarms the timer, so a turn-driven post would be a cheerful
    footnote to an ending — and the ordinary case is exactly that, since the
    signature closes the readiness item and the next diff would fire `clear`.
    """
    from inspect_steward._tend import turn_post

    workspace = done(tmp_path)
    turn(workspace)
    sign(workspace)

    assert turn_post(turn(workspace)) is None


def test_the_history_names_the_signature_and_what_it_moved(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK, created="2026-01-01T00:00:00Z", completed=2)
    write_log(workspace.logs, TASK, created="2026-01-02T00:00:00Z")

    sign(workspace, note="ship it")
    text = "\n".join(entry.text for entry in turn(workspace).happened.entries)

    assert "signed off by kaia" in text
    assert "ship it" in text
    assert "curated 1 superseded attempt" in text


# --- the command -----------------------------------------------------------


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    workspace = done(tmp_path)
    monkeypatch.chdir(workspace.root)
    return workspace


def test_the_command_signs_and_says_what_is_still_the_humans(here: Workspace) -> None:
    result = run("signoff", "--by", "kaia")

    assert result.exit_code == 0, result.output
    assert "🔒 signed off by kaia" in result.output
    assert "no accepted exceptions" in result.output
    # the one thing signoff does not do, said on the way out
    assert "yours to commit" in result.output


def test_the_command_prints_every_blocker_and_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_workspace(tmp_path, git=False)
    workspace = erroring(tmp_path, errors=2, samples=4)
    monkeypatch.chdir(workspace.root)

    result = run("signoff", "--by", "kaia")

    assert result.exit_code != 0
    assert "cannot be signed yet" in result.output
    assert "steward rule" in result.output
    assert "nothing was signed" in result.output


def test_the_command_refuses_a_workspace_nobody_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "journal.jsonl").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = run("signoff", "--by", "kaia")

    assert result.exit_code != 0
    assert "nothing has been launched" in result.output


def test_the_json_output_branches_on_one_field(here: Workspace) -> None:
    import json

    result = run("signoff", "--by", "kaia", "--json")

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["signed"] is True
    assert document["signature"]["by"] == "kaia"
    assert document["blockers"] == []


def test_a_signature_with_nobody_behind_it_is_refused(here: Workspace) -> None:
    """`required` means present, not somebody.

    An empty name signed, curated, disarmed the timer and printed a success —
    and `read_signoff` then discarded the event, so the run was left with no
    attestation, no timer, and nothing left to notice that.
    """
    result = run("signoff", "--by", "   ")

    assert result.exit_code != 0
    assert "not an attestation" in result.output
    assert read_signoff(read_journal(here.journal).events) is None
    assert turn(here).verdict is not Verdict.SIGNED_OFF


def test_acking_the_invitation_is_refused_and_names_the_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It was acknowledgeable for as long as there was no verb to run.

    An ack now would record *I have decided* in the one place the decision is
    not: the run would go quiet with no signature, no curation, and nothing in
    `anomalies.md` marked final.
    """
    workspace = done(tmp_path)
    monkeypatch.chdir(workspace.root)

    result = run("ack", "signoff", "--reason", "the scores look right")

    assert result.exit_code != 0
    assert "steward signoff --by NAME" in result.output


def test_an_ack_recorded_before_the_verb_existed_no_longer_silences_it(
    tmp_path: Path,
) -> None:
    """The migration hazard, and the reason the filter narrowed.

    A workspace where somebody silenced this in October holds an ack whose id
    still matches. Filtering on the id alone would leave that run quiet forever
    and never offer it the command it was waiting for.
    """
    from inspect_steward._workspace import ACKNOWLEDGED

    workspace = done(tmp_path)
    ready = next(item for item in turn(workspace).items if item.kind == "signoff_ready")
    append_event(
        workspace.journal, ACKNOWLEDGED, id=ready.id, by="human", reason="accepted"
    )

    assert any(item.kind == "signoff_ready" for item in turn(workspace).items)


# --- the scan bracket's terminal half ---------------------------------------


def scanned(root: Path) -> Workspace:
    """A signed-off-ready run whose scan directory holds one dismissed finding."""
    from ..anomaly.test_scan_items import (
        CLASS as SCAN_CLASS,
    )
    from ..anomaly.test_scan_items import (
        MATERIAL,
        SCAN_ID,
        land_it,
    )

    create_workspace(root, git=False)
    workspace, manifest = prepared(root, [TASK])
    write_manifest(
        manifest.model_copy(update={"scan": MATERIAL, "eval_set_id": SCAN_ID}),
        workspace.manifest,
    )
    initialize_scan(MATERIAL, log_dir=str(workspace.logs), scan_id=SCAN_ID)
    land_it(workspace)
    turn(workspace)
    ruling(workspace, "dismiss", **{"class": SCAN_CLASS})
    return workspace


def test_signing_folds_the_last_rows_and_marks_the_scan_complete(
    tmp_path: Path,
) -> None:
    """The one moment the prune is honest: nothing is running, and this process is the only writer.

    Every tend folds with `complete=False` — deliberately, since a sibling worker might still be recording — so the flag is the observable difference between the two halves of the bracket.
    """
    import json

    from ..anomaly.test_scan_items import SCAN_ID

    workspace = scanned(tmp_path)
    scan_dir = Path(
        scan_dir_location(log_dir=str(workspace.logs), scan_id=SCAN_ID, scans=None)
    )
    assert json.loads((scan_dir / "_summary.json").read_text())["complete"] is False

    result = sign(workspace)

    assert result.blockers == []
    assert json.loads((scan_dir / "_summary.json").read_text())["complete"] is True


def test_a_finalize_that_fails_refuses_and_signs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike a move that failed, and the difference is what it costs.

    `_rerender`'s reasoning — a filesystem that will not cooperate must not unmake a decision a person made — does not reach this call, which runs *before* the `SIGNOFF` event and so unmakes nothing by stopping. What it costs is not tidiness: rows nothing compacted are rows the census never read, so a signature taken over them says *nothing was flagged* about samples nothing has looked at.
    """
    import importlib

    module = importlib.import_module("inspect_steward._signoff.sign")
    workspace = scanned(tmp_path)

    def refuses(**kwargs: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(module, "finalize_scan", refuses)
    result = sign(workspace)

    assert kinds(result) == [UNFINALIZED]
    assert result.signature is None
    assert read_signoff(read_journal(workspace.journal).events) is None
    # and it is retryable rather than terminal, which is what keeps a
    # transient store failure from being a run nobody can ever sign
    assert "run it again" in result.blockers[0].remedy


def test_a_finding_the_terminal_fold_uncovers_un_signs_the_signature(
    tmp_path: Path,
) -> None:
    """The one window between the gate and the return, and what is on the other side of it.

    Every tend's fold can have failed — or, as here, never been due — so the terminal finalize is the first thing to compact a row, and it runs *after* the gate has passed. Reporting success then would leave a signature in the journal, a run that reads unsigned, a disarmed timer and nothing left to notice.
    """
    from ..anomaly.test_scan_items import (
        CLASS as SCAN_CLASS,
    )
    from ..anomaly.test_scan_items import (
        MATERIAL,
        SCAN_ID,
        record,
    )

    create_workspace(tmp_path, git=False)
    workspace, manifest = prepared(tmp_path, [TASK])
    write_manifest(
        manifest.model_copy(update={"scan": MATERIAL, "eval_set_id": SCAN_ID}),
        workspace.manifest,
    )
    initialize_scan(MATERIAL, log_dir=str(workspace.logs), scan_id=SCAN_ID)
    # real sample uuids, because the terminal finalize prunes rows no surviving
    # log accounts for — the row has to be one the finalize keeps
    samples = [SynthSample(id=f"s{index}") for index in range(4)]
    log = write_log(workspace.logs, TASK, samples=samples)
    record(
        workspace, str(log), uuid=samples[0].uuid, value=True, label="reward_hacking"
    )

    # nothing has run since the row was buffered, so no tend was ever due to
    # fold it and the gate sees a clean run
    assert check(turn(workspace), None) == []

    result = sign(workspace)

    # and the reason travels with it: `unsigned` says the signature did not
    # survive, and the gate re-run over the post-finalize state says what of
    assert kinds(result) == [UNSIGNED, OPEN_WINDOW]
    assert SCAN_CLASS in result.blockers[1].summary
    assert result.signature is None
    assert [anomaly.class_key for anomaly in result.turn.anomalies.open] == [SCAN_CLASS]


def test_an_unreadable_result_the_finalize_leaves_behind_is_not_signed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other thing the terminal fold can reveal, and the signature stands over neither.

    A window that opens un-signs the run and is caught by `signed_off` alone; results that will not read leave the signature standing and the run reading signed, with a hole in it nobody would ever be shown. One reason a run cannot be signed is every reason, so the gate is asked again rather than one predicate of it.
    """
    import importlib

    from ..anomaly.test_scan_items import corrupt

    module = importlib.import_module("inspect_steward._signoff.sign")
    workspace = scanned(tmp_path)
    real = module.finalize_scan

    def damages(**kwargs: Any) -> Any:
        # the compaction is what writes the file the census then projects, so
        # a truncated write here is the shape of this failure
        summary = real(**kwargs)
        corrupt(workspace)
        return summary

    monkeypatch.setattr(module, "finalize_scan", damages)
    result = sign(workspace)

    assert kinds(result) == [UNSIGNED, UNREAD]
    assert result.signature is None
    assert result.disarmed is None


def test_a_scan_nothing_could_re_read_leaves_the_timer_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-finalize check is the only thing that ever looks at what the fold compacted.

    A turn that raised leaves that question open rather than answered, and taking the timer down is what would turn *unverified* into *unnoticeable*. So the signature stands — it is in the journal — and the two acts that assume it was verified do not happen: the run keeps tending, and the caller is told this one is not finished.
    """
    import importlib

    module = importlib.import_module("inspect_steward._signoff.sign")
    workspace = scanned(tmp_path)
    append_event(workspace.journal, ARMED, scheduler="cron", interval=600, label="w")
    real = module.tend
    turns = 0

    def once(*args: Any, **kwargs: Any) -> Any:
        nonlocal turns
        turns += 1
        if turns > 1:
            raise OSError("the journal went away between the two turns")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "tend", once)
    result = sign(workspace)

    assert result.signature is not None
    assert result.unverified is not None
    assert result.disarmed is None
    assert turn(workspace).supervision is not None
    assert turn(workspace).supervision.armed is not None  # type: ignore[union-attr]


def test_a_final_turn_that_cannot_run_leaves_the_signature_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that would not run is not a run that was un-signed, and nothing about the run says which.

    The pre-signature turn is what `_rerender` hands back when its own turn raises, and that turn reads unsigned because it ran before the signature existed. Testing it for the signature would report the attestation invalidated over an attestation sitting in the journal — and then leave the timer armed and tell the person nothing was signed.
    """
    import importlib

    module = importlib.import_module("inspect_steward._signoff.sign")
    workspace = done(tmp_path)
    append_event(workspace.journal, ARMED, scheduler="cron", interval=600, label="w")
    real = module.tend
    turns = 0

    def once(*args: Any, **kwargs: Any) -> Any:
        nonlocal turns
        turns += 1
        if turns > 1:
            raise OSError("the journal went away between the two turns")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "tend", once)
    result = sign(workspace)

    assert result.blockers == []
    assert result.signature is not None
    assert result.disarmed == "cron"
    assert read_signoff(read_journal(workspace.journal).events) is not None
    assert any("the final turn could not run" in warning for warning in result.warnings)


def test_a_finalize_that_worked_stops_the_last_turn_folding_over_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fold episode's one bad ending: a retry that arrives after the terminal act.

    The episode exists so a fold that failed on a departure turn keeps being retried while nothing is running — which is right up to the moment the finalize lands. After it the buffer has been cleaned and the summary rebuilt from the rows, and `_rerender`'s turn would fold once more with `complete=False`, overwriting that summary with an incomplete one counted off a buffer that is no longer there.

    The signoff turn's fold failing and the finalize then working is the one arrangement that leaves the episode open across the finalize — a transient refusal, which is what the one-shot failure here stands for.
    """
    import importlib
    import json

    from ..anomaly.test_scan_items import SCAN_ID

    turn_module = importlib.import_module("inspect_steward._tend.turn")
    workspace = scanned(tmp_path)
    scan_dir = Path(
        scan_dir_location(log_dir=str(workspace.logs), scan_id=SCAN_ID, scans=None)
    )
    real = turn_module.sync_scan
    folds = 0

    def refuses_once(**kwargs: Any) -> None:
        nonlocal folds
        folds += 1
        if folds == 1:
            raise OSError("the store would not answer")
        real(**kwargs)

    monkeypatch.setattr(turn_module, "sync_scan", refuses_once)
    append_event(
        workspace.journal, ACTION, action=SCAN_FOLD_FAILED, target=str(scan_dir)
    )

    result = sign(workspace)

    assert result.signature is not None
    assert folds == 1, "the last turn folded over a scan the finalize had completed"
    assert json.loads((scan_dir / "_summary.json").read_text())["complete"] is True


def test_a_run_that_scans_nothing_finalizes_nothing(tmp_path: Path) -> None:
    workspace = done(tmp_path)

    result = sign(workspace)

    assert result.blockers == []
    assert not any("finalized" in warning for warning in result.warnings)


# --- there has to be something to attest to ------------------------------


class TestNothingToSign:
    """The gate asked whether anything was *unfinished*, and nothing is unfinished about nothing.

    A capture that enumerated no tasks reports zero missing and zero incomplete, so an empty manifest passed every refusal and received a signature attesting to an empty set — a definition edited to nothing, an argument that filtered every task away, a `-T` typo. The readiness *item* already had the guard, which is the tell: the invitation knew there was nothing to invite anybody to, and the verb did not require the invitation.
    """

    def test_a_capture_with_no_tasks_cannot_be_signed(self, tmp_path: Path) -> None:
        create_workspace(tmp_path, git=False)
        workspace, _ = prepared(tmp_path, [])

        result = sign(workspace)

        assert kinds(result) == [EMPTY]
        assert result.signature is None
        assert "no tasks" in result.blockers[0].summary

    def test_tasks_that_finished_and_produced_no_samples_cannot_be_signed(
        self, tmp_path: Path
    ) -> None:
        # the same hole one level down: every count is zero and every count
        # agrees, so a `0/0` log that landed `success` satisfied the gate
        create_workspace(tmp_path, git=False)
        workspace, _ = prepared(tmp_path, [EMPTY_TASK])
        write_log(workspace.logs, EMPTY_TASK, samples=[])

        result = sign(workspace)

        assert kinds(result) == [EMPTY]
        assert "not one sample" in result.blockers[0].summary

    def test_a_run_that_has_not_started_is_unsettled_rather_than_empty(
        self, tmp_path: Path
    ) -> None:
        # it has no samples either, and `UNSETTLED` is already its answer --
        # two refusals for one condition is the loop this gate collapses
        create_workspace(tmp_path, git=False)
        workspace, _ = prepared(tmp_path, [TASK])

        assert kinds(sign(workspace)) == [UNSETTLED]

    def test_a_run_with_results_is_unaffected(self, tmp_path: Path) -> None:
        assert kinds(sign(done(tmp_path))) == []


# --- what the scanners actually reached, in the signature ----------------


class TestTheCoverageTheSignatureWasTakenOver:
    """A signature reading *no accepted exceptions* over a census that covers none of the run.

    Nothing recorded a coverage shortfall: `finalize_scan`'s return was dropped on the floor and the gate never read `result.coverage`, so a completed four-sample run with zero transcripts scanned produced an `exceptions=()` in the journal and a terminal line saying so. Not a refusal — nothing in Steward closes a coverage gap and several correct configurations produce one — but the number belongs in the record the signature is.
    """

    def test_a_short_census_is_named_in_the_signature(self, tmp_path: Path) -> None:
        workspace = scanned(tmp_path)

        result = sign(workspace)

        assert result.signature is not None
        assert any(
            one.startswith("scan coverage:") for one in result.signature.exceptions
        )
        assert "of 4 transcripts" in " ".join(result.signature.exceptions)

    def test_it_reaches_the_journal_too(self, tmp_path: Path) -> None:
        # the signature is rebuilt from the journal on every later read, so a
        # caveat that lives only in the returned object is a caveat that is gone
        # by morning
        workspace = scanned(tmp_path)

        sign(workspace)

        recorded = read_signoff(read_journal(workspace.journal).events)
        assert recorded is not None
        assert any(one.startswith("scan coverage:") for one in recorded.exceptions)

    def test_it_does_not_refuse(self, tmp_path: Path) -> None:
        # `--accept-coverage` was tried and taken back out: `coverage` counts a
        # transcript only once *every* scanner has answered, so a filtered
        # scanner reads as zero coverage on a run that scans perfectly well.
        # A flag every signoff has to carry is the same as no gate
        assert kinds(sign(scanned(tmp_path))) == []

    def test_a_run_that_scans_nothing_records_no_such_exception(
        self, tmp_path: Path
    ) -> None:
        result = sign(done(tmp_path))

        assert result.signature is not None
        assert result.signature.exceptions == ()
