"""`--publish` — the one act at the end of a run that nothing does by default.

A store row is a claim that a result may be reused sight-unseen, which is the same claim signoff makes, which is why the two are one command rather than two. Nothing is published as logs land: with `fail_on_error=False` a task finishes `status="success"` while carrying errored samples, so a freshly landed log is exactly the provisional thing adjudication exists to examine (execution.md §5.5).

**And nothing is published without being asked.** There is no `_steward.yaml` key that turns this on, because a key that could say `true` is publication nobody was asked about — so the flag is the whole surface, and the readiness item is what makes somebody aware there is a decision waiting.
"""

import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from inspect_ai._util.file import FileSystem
from inspect_steward._signoff.sign import WITHDRAWALS
from inspect_steward._store import DirectoryStore, StoreError, directory
from inspect_steward._tend.items import SIGNOFF_READY, Item
from inspect_steward._workspace import Workspace, create_workspace, read_journal

from .._logs import SynthTask, write_log
from ..anomaly.test_items import CLASS, erroring
from ..schedule.test_tend import turn
from .test_signoff import TASK, done, logs, ruling, sign

OTHER = SynthTask("second", samples=2)


def configured(workspace: Workspace, location: Path) -> None:
    """Point the workspace's `_steward.yaml` at a store."""
    workspace.directives.write_text(f"log_store: {location}\n", encoding="utf-8")


def ready(workspace: Workspace) -> Item:
    """The `signoff_ready` item a turn raises over a finished run."""
    items = [one for one in turn(workspace).items if one.kind == SIGNOFF_READY]
    assert len(items) == 1, items
    return items[0]


def published_event(workspace: Workspace) -> dict[str, Any] | None:
    for event in read_journal(workspace.journal).events:
        if event.type == "action" and event.payload.get("action") == "published":
            return event.payload
    return None


def withdrawals(workspace: Workspace) -> list[dict[str, Any]]:
    """Every unpaid-withdrawal snapshot in the journal, oldest first."""
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == "action" and event.payload.get("action") == WITHDRAWALS
    ]


def superseded(workspace: Workspace) -> Path:
    """A second, older attempt of a live task — which curation archives.

    An *orphan* would not do: the turn's own reconcile archives one the moment it meets it, so by signoff there is nothing left for curation to move.
    """
    return write_log(workspace.logs, TASK, created="2020-01-01T00:00:00+00:00")


def published_then_superseded(tmp_path: Path) -> tuple[Workspace, Path, Path]:
    """A workspace that published a log at one signoff and superseded it before the next.

    **The publication has to be a real signoff rather than a call on the store**, which is the whole of what provenance changed. Withdrawal works from what *this workspace's journal says it wrote*, so a log that reached the store some other way — a colleague's publication, or this run reusing it from there — is not this project's to take back out. A fixture that put it there directly was asserting a withdrawal that must no longer happen.

    Returns:
        The workspace, the store, and the log that is now superseded.
    """
    workspace = done(tmp_path, TASK)
    store = tmp_path / "store"
    configured(workspace, store)
    sign(workspace, publish=True)
    published = logs(workspace)[0]
    # newer, so the log just published becomes the one curation archives
    write_log(workspace.logs, TASK, created="2030-01-01T00:00:00+00:00")
    return workspace, store, published


def flaky_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the first copy of a publication and let the rest land."""
    real = directory.copy_log
    attempted: list[str] = []

    def flaky(source: str, target: str, fs: FileSystem) -> bool:
        attempted.append(source)
        if len(attempted) == 1:
            raise PermissionError("the store said no")
        return real(source, target, fs)

    monkeypatch.setattr(directory, "copy_log", flaky)


def refuse_withdrawal(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Make withdrawal fail, and hand back the way to let it succeed again.

    **A toggle rather than `monkeypatch.undo()`**, which was the first shape of this and reached much further than it looked. The `monkeypatch` fixture is function-scoped and shared with everything autouse around the test, so undoing "the patch this test made" also undid `no_ambient_channel` — putting the developer's own `STEWARD_NOTIFICATION` back from `.env`, and posting a real Slack message from the signoff that came next.

    Returns:
        A callable that stops the refusal, for a test asserting the retry.
    """
    refusing = [True]
    real = DirectoryStore.withdraw

    def refuse(self: DirectoryStore, locations: Sequence[str]) -> None:
        if refusing[0]:
            raise StoreError("the rows would not come out")
        real(self, locations)

    monkeypatch.setattr(DirectoryStore, "withdraw", refuse)

    def relent() -> None:
        refusing[0] = False

    return relent


class TestPublishingWhatWasSigned:
    def test_the_signed_logs_reach_the_store(self, tmp_path: Path) -> None:
        workspace = done(tmp_path, TASK, OTHER)
        store = tmp_path / "store"
        configured(workspace, store)

        result = sign(workspace, publish=True)

        assert result.signature is not None
        assert result.published is not None
        assert result.published.count == 2
        assert result.published.kind == "copied"

    def test_and_are_findable_by_identifier_afterwards(self, tmp_path: Path) -> None:
        # the round trip the feature exists for: another project's launch asks
        # this question and does not run the task
        workspace = done(tmp_path, TASK)
        store = tmp_path / "store"
        configured(workspace, store)

        sign(workspace, publish=True)

        found = DirectoryStore(str(store)).search({TASK.identifier})
        assert set(found) == {TASK.identifier}

    def test_the_store_and_the_count_are_journaled(self, tmp_path: Path) -> None:
        workspace = done(tmp_path, TASK)
        store = tmp_path / "store"
        configured(workspace, store)

        sign(workspace, publish=True)

        event = published_event(workspace)
        assert event is not None
        assert event["store"] == str(store)
        assert event["logs"] == 1

    def test_a_task_carrying_an_accepted_exception_is_published_too(
        self, tmp_path: Path
    ) -> None:
        # **the sharpest open question, answered the permissive way.** A
        # signature is a signature: two samples accepted as errored is a
        # legitimate result with a caveat, and the caveat lives in this
        # project's `anomalies.md` and travels nowhere. That is a hole, and it
        # is accepted knowingly -- withholding results a person explicitly
        # accepted would make the store lie in the other direction
        workspace = erroring(tmp_path, errors=2, samples=4)
        turn(workspace)
        store = tmp_path / "store"
        configured(workspace, store)
        ruling(workspace, "exclude", effect="2 samples excluded from scoring")

        result = sign(workspace, publish=True)

        assert result.signature is not None
        assert result.signature.exceptions == (CLASS,)
        assert result.published is not None
        assert result.published.count == 1


class TestOnlyWhatTheSignatureCovers:
    """The publish set is `logs/` *after* curation, which takes two filters rather than none.

    The observation in hand was read before the moves. An orphan is the sharp case: an identifier the definition no longer names has a current log, `plan` archives every one of its attempts including that one, and the signature covers none of them. Publishing straight off the observation exported results the attestation excludes — and where the move had already landed, failed partway through the batch on a path that was no longer there, after copying some of the valid logs.
    """

    def orphaned(self, tmp_path: Path) -> Workspace:
        """A signable run with a log for a task the manifest no longer names."""
        workspace = done(tmp_path, TASK)
        write_log(workspace.logs, SynthTask("departed", samples=2))
        return workspace

    def test_an_orphans_log_is_not_published(self, tmp_path: Path) -> None:
        workspace = self.orphaned(tmp_path)
        store = tmp_path / "store"
        configured(workspace, store)

        result = sign(workspace, publish=True)

        assert result.signature is not None
        assert result.published is not None
        assert result.published.count == 1
        assert [one.name for one in store.iterdir() if one.is_file()] == [
            Path(logs(workspace)[0]).name
        ]

    def test_a_failed_withdrawal_does_not_erase_a_publication_that_worked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # **the two used to share one `try`.** A withdrawal that failed after a
        # publication that succeeded returned `None`, wrote no journal event,
        # and told the operator nothing had been published — about a store that
        # was at that moment holding every new log
        workspace, _, _ = published_then_superseded(tmp_path)
        refuse_withdrawal(monkeypatch)

        result = sign(workspace, publish=True, again=True)

        assert result.published is not None
        assert result.published.count == 1
        assert published_event(workspace) is not None
        # and the tidy-up left undone is its own warning, in its own words
        assert any("could not be withdrawn" in one for one in result.warnings)

    def test_and_the_valid_logs_still_reach_the_store(self, tmp_path: Path) -> None:
        # the second half of the same defect: curation moves the orphan first,
        # so publishing from the stale observation hit a path that was no longer
        # there and took the rest of the batch down with it
        workspace = self.orphaned(tmp_path)
        store = tmp_path / "store"
        configured(workspace, store)

        sign(workspace, publish=True)

        assert set(DirectoryStore(str(store)).search({TASK.identifier})) == {
            TASK.identifier
        }


class TestWithdrawalIsNotPublicationsToAuthorise:
    """`--publish` gates putting results *in*. Taking a superseded one *out* is not the same permission.

    They were gated together — the store reached publication as `store if publish else None` — so a signoff that published last month and this month curates that attempt away without the flag left the store handing out the log this project had just replaced, for as long as nobody typed it. Publication exports results and is a decision; withdrawal removes rows this project itself wrote for attempts it has just archived, exports nothing, and is owed whether or not anybody is publishing today.
    """

    def test_a_superseded_log_is_withdrawn_with_no_publish_flag(
        self, tmp_path: Path
    ) -> None:
        workspace, store, old = published_then_superseded(tmp_path)

        result = sign(workspace, again=True)

        assert result.published is None
        assert not (store / old.name).exists()
        assert (store / "withdrawn" / old.name).exists()

    def test_and_the_store_stops_handing_it_out(self, tmp_path: Path) -> None:
        # the consequence the withdrawal exists for, asserted through the query
        # another project's launch actually makes. The replacement is still
        # unpublished, so the identifier goes unanswered rather than answered
        # with the log this project just replaced
        workspace, store, _ = published_then_superseded(tmp_path)

        sign(workspace, again=True)

        assert DirectoryStore(str(store)).search({TASK.identifier}) == {}

    def test_a_signoff_with_nothing_owed_does_not_touch_the_store(
        self, tmp_path: Path
    ) -> None:
        # nothing to put in and nothing to take out, so a signoff nobody asked
        # to publish must not fail over a store it had no business opening
        workspace = done(tmp_path, TASK)
        blocked = tmp_path / "store"
        blocked.write_text("not a directory", encoding="utf-8")
        configured(workspace, blocked)

        result = sign(workspace)

        assert result.signature is not None
        assert result.warnings == []


class TestOnlyWhatThisProjectPublished:
    """A withdrawal has to be tied to the publication that created it, and it was tied to nothing.

    `curated.moved` was withdrawn from whatever `log_store` said today, which failed in both directions at once. A workspace repointed from one store to another left the first store's row standing forever, because nothing would ever ask that store about it again. And a directory store matches on the log's own filename — so a project that **reused** a log from a shared store and later archived that attempt moved the *producer's* copy into `withdrawn/`, ending reuse of it for everybody, over a log it never published.

    So the journal is the authority. It needs no publisher field to be one: it is this workspace's journal, so everything in it was written here — what it could not answer was *which logs*, which is now recorded by name.
    """

    def test_a_log_the_store_already_had_is_not_this_projects_to_withdraw(
        self, tmp_path: Path
    ) -> None:
        # the reuse case, and the reason `written` exists: a log copied *in*
        # from a shared store carries the store's own filename, so publishing it
        # back finds the name taken and skips — published, by its producer
        workspace = done(tmp_path, TASK)
        store = tmp_path / "store"
        configured(workspace, store)
        theirs = logs(workspace)[0]
        store.mkdir()
        shutil.copy(theirs, store / theirs.name)

        sign(workspace, publish=True)
        write_log(workspace.logs, TASK, created="2030-01-01T00:00:00+00:00")
        sign(workspace, again=True)

        assert (store / theirs.name).exists()
        assert not (store / "withdrawn").exists()

    def test_and_the_producers_copy_is_still_reusable(self, tmp_path: Path) -> None:
        # the consequence, through the query another project's launch makes
        workspace = done(tmp_path, TASK)
        store = tmp_path / "store"
        configured(workspace, store)
        theirs = logs(workspace)[0]
        store.mkdir()
        shutil.copy(theirs, store / theirs.name)

        sign(workspace, publish=True)
        write_log(workspace.logs, TASK, created="2030-01-01T00:00:00+00:00")
        sign(workspace, again=True)

        assert set(DirectoryStore(str(store)).search({TASK.identifier})) == {
            TASK.identifier
        }

    def test_a_store_no_longer_configured_still_gets_its_own_row_back(
        self, tmp_path: Path
    ) -> None:
        # the other direction: repointing a workspace used to strand the first
        # store's row permanently, since nothing would ask that store again
        workspace, store, old = published_then_superseded(tmp_path)
        configured(workspace, tmp_path / "elsewhere")

        sign(workspace, again=True)

        assert (store / "withdrawn" / old.name).exists()

    def test_the_publication_is_journaled_by_name(self, tmp_path: Path) -> None:
        # a count reports a publication and cannot undo one -- withdrawal has to
        # know which store holds which log because this project put it there
        workspace = done(tmp_path, TASK)
        configured(workspace, tmp_path / "store")

        sign(workspace, publish=True)

        event = published_event(workspace)
        assert event is not None
        # a `file://` URI, which is the form `curated.moved` reports too — the
        # two are compared directly, so they have to be the one spelling
        assert [Path(one).name for one in event["written"]] == [logs(workspace)[0].name]


class TestAWithdrawalThatDidNotHappen:
    """It was called *the next signoff's problem*, which was a promise with no mechanism.

    Curation has already moved the log to `logs-archive/` by the time withdrawal is attempted, and `plan` works from what `logs/` holds — so no later signoff could rediscover it and no later run would ever try again. The store would go on serving a superseded result with the only trace of it a warning that scrolled past once.
    """

    def owed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Workspace, Callable[[], None]]:
        """A workspace whose withdrawal was refused, and the way to let the retry succeed."""
        workspace, _, _ = published_then_superseded(tmp_path)
        relent = refuse_withdrawal(monkeypatch)
        sign(workspace, again=True)
        return workspace, relent

    def test_the_debt_is_written_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, _ = self.owed(tmp_path, monkeypatch)

        recorded = withdrawals(workspace)
        assert len(recorded) == 1
        assert recorded[0]["store"] == str(tmp_path / "store")
        assert len(recorded[0]["logs"]) == 1

    def test_and_the_warning_says_it_will_be_tried_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, _, _ = published_then_superseded(tmp_path)
        refuse_withdrawal(monkeypatch)

        result = sign(workspace, again=True)

        assert any("could not be withdrawn" in one for one in result.warnings)
        assert any("tries again" in one for one in result.warnings)

    def test_the_next_signoff_finishes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the whole point of writing it down: the log is in `logs-archive/` and
        # out of every later signoff's reach, so the retry can only come from
        # the ledger
        workspace, relent = self.owed(tmp_path, monkeypatch)
        store = tmp_path / "store"
        name = sorted(one.name for one in store.iterdir() if one.is_file())[0]
        relent()

        result = sign(workspace, again=True)

        assert not (store / name).exists()
        assert (store / "withdrawn" / name).exists()
        assert not any("could not be withdrawn" in one for one in result.warnings)

    def test_and_stops_owing_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # a paid debt is closed out with an empty snapshot, which is what ends
        # the fold -- otherwise every later signoff would withdraw it forever
        workspace, relent = self.owed(tmp_path, monkeypatch)
        relent()

        sign(workspace, again=True)

        assert withdrawals(workspace)[-1]["logs"] == []

    def test_a_signoff_that_owes_nothing_writes_no_ledger_entry(
        self, tmp_path: Path
    ) -> None:
        # the ordinary signoff, whose journal a person reads
        workspace, _, _ = published_then_superseded(tmp_path)

        sign(workspace, again=True)

        assert withdrawals(workspace) == []

    def test_a_debt_owed_to_another_store_is_not_this_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the ledger is keyed on the store, so repointing the workspace does not
        # hand one store's debt to another. The second store deliberately holds
        # a log of the *same name*, which is what a leaked ledger would reach
        # for — withdrawal matches on the filename publication wrote
        workspace, relent = self.owed(tmp_path, monkeypatch)
        relent()
        store = tmp_path / "store"
        name = sorted(one.name for one in store.iterdir() if one.is_file())[0]
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        shutil.copy(store / name, elsewhere / name)
        configured(workspace, elsewhere)

        sign(workspace, again=True)

        assert (elsewhere / name).exists()
        assert not (elsewhere / "withdrawn").exists()


class TestNothingLeavesAnUnfinishedRun:
    """The one window between the gate and the return, and why publication sits behind it.

    Every tend's fold can have failed — or never been due — so the terminal finalize can be the first thing to compact a scan row, and it runs *after* the gate passed. A window it uncovers un-signs the signature: the verb returns blockers and reports that nothing was signed. Publishing before that check exported the results into a shared cache anyway, and *then* told the operator the run was not signed — which is the failure execution.md §5.5 exists to prevent, reached through the one door left open.
    """

    def terminal_finding(self, tmp_path: Path) -> Workspace:
        """A run the gate passes and the terminal fold puts a window in front of."""
        from inspect_steward._evalset.manifest import write_manifest
        from inspect_steward._scan import initialize_scan

        from .._logs import SynthSample
        from ..anomaly.test_scan_items import MATERIAL, SCAN_ID, record
        from ..schedule.test_tend import prepared

        create_workspace(tmp_path, git=False)
        workspace, manifest = prepared(tmp_path, [TASK])
        write_manifest(
            manifest.model_copy(update={"scan": MATERIAL, "eval_set_id": SCAN_ID}),
            workspace.manifest,
        )
        initialize_scan(MATERIAL, log_dir=str(workspace.logs), scan_id=SCAN_ID)
        samples = [SynthSample(id=f"s{index}") for index in range(4)]
        log = write_log(workspace.logs, TASK, samples=samples)
        record(
            workspace,
            str(log),
            uuid=samples[0].uuid,
            value=True,
            label="reward_hacking",
        )
        turn(workspace)
        return workspace

    def test_a_window_the_finalize_uncovers_publishes_nothing(
        self, tmp_path: Path
    ) -> None:
        workspace = self.terminal_finding(tmp_path)
        store = tmp_path / "store"
        configured(workspace, store)

        result = sign(workspace, publish=True)

        # the run is not signed, so nothing about it may be reused
        assert result.signature is None
        assert result.published is None
        assert not store.exists()
        assert published_event(workspace) is None

    def test_and_says_why_it_held_them_back(self, tmp_path: Path) -> None:
        workspace = self.terminal_finding(tmp_path)
        configured(workspace, tmp_path / "store")

        result = sign(workspace, publish=True)

        assert result.unpublished is not None
        assert "not finished" in result.unpublished


class TestARelativeStoreIsRelativeToTheWorkspace:
    """A Steward command runs from anywhere at or below the workspace, and only the setting is fixed.

    `log_store` is resolved at launch and again at signoff, so a relative one used to name a different directory to each command depending on where it was typed — publication going to one place and the next run's reuse looking in another, with nothing to say the two had disagreed.
    """

    def test_publication_lands_beside_the_workspace_not_the_caller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = done(tmp_path, TASK)
        workspace.directives.write_text("log_store: ./store\n", encoding="utf-8")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        result = sign(workspace, publish=True)

        assert result.published is not None
        assert result.published.count == 1
        assert set(DirectoryStore(str(tmp_path / "store")).search({TASK.identifier}))
        assert not (elsewhere / "store").exists()

    def test_and_what_is_reported_is_the_place_rather_than_the_setting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the identity a warning names, the journal records, and the withdrawal
        # ledger matches a debt against — one place, spelled one way
        workspace = done(tmp_path, TASK)
        workspace.directives.write_text("log_store: ./store\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path / "logs")

        result = sign(workspace)

        assert result.unpublished is not None
        assert str(tmp_path / "store") in result.unpublished


class TestBeingAskedInTheFirstPlace:
    """The readiness item, which is the whole mechanism by which anybody decides.

    Publication is the one act at the end of a run that nothing does by default and no setting can turn on, so an agent that is not told there is a store is an agent that signs off without mentioning it — and the run never tends again to say so afterwards.
    """

    def test_the_invitation_names_the_store_and_the_flag(self, tmp_path: Path) -> None:
        workspace = done(tmp_path, TASK)
        store = tmp_path / "store"
        configured(workspace, store)

        item = ready(workspace)

        assert str(store) in item.summary
        assert "--publish" in item.summary
        assert "ask whether" in item.summary

    def test_and_says_nothing_where_there_is_no_store(self, tmp_path: Path) -> None:
        # no store, no decision, nothing to ask about
        item = ready(done(tmp_path, TASK))

        assert "--publish" not in item.summary
        assert item.summary.endswith("waiting to be accepted")

    def test_the_run_is_still_what_the_line_is_mostly_about(
        self, tmp_path: Path
    ) -> None:
        # the store clause is appended to the readiness sentence rather than
        # replacing it: the decision owed is still *accept these results*
        workspace = done(tmp_path, TASK)
        configured(workspace, tmp_path / "store")

        assert "waiting to be accepted" in ready(workspace).summary


class TestNotPublishing:
    def test_a_signoff_that_was_not_asked_publishes_nothing(
        self, tmp_path: Path
    ) -> None:
        workspace = done(tmp_path, TASK)
        store = tmp_path / "store"
        configured(workspace, store)

        result = sign(workspace)

        assert result.published is None
        assert not store.exists()

    def test_but_it_says_a_store_is_sitting_there(self, tmp_path: Path) -> None:
        # **the last moment anybody is looking.** The timer comes down and a
        # signed run never tends again, so a store configured and unwritten
        # either says so here or is never mentioned
        workspace = done(tmp_path, TASK)
        store = tmp_path / "store"
        configured(workspace, store)

        result = sign(workspace)

        assert result.unpublished is not None
        assert str(store) in result.unpublished
        assert "--publish" in result.unpublished

    def test_and_says_nothing_where_no_store_is_configured(
        self, tmp_path: Path
    ) -> None:
        result = sign(done(tmp_path, TASK))

        assert result.unpublished is None
        assert result.published is None

    def test_publish_with_nowhere_to_publish_says_so(self, tmp_path: Path) -> None:
        # **it used to succeed in silence.** `--publish` with no store resolved
        # published nothing *and* suppressed the line that would have mentioned
        # a store, on the grounds that the operator had already decided -- so
        # the signoff disarmed and said nothing at all
        workspace = done(tmp_path, TASK)

        result = sign(workspace, publish=True)

        assert result.signature is not None
        assert result.published is None
        assert any("--publish was given" in one for one in result.warnings)
        assert any("log_store" in one for one in result.warnings)

    def test_a_partial_publication_names_what_did_not_land(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # **it raised, and the caller reported *nothing was published* about a
        # store already holding the rest.** The copies are sequential and
        # nothing wraps them, so the count is what the signature covers and the
        # failures are what somebody has to go and look at — neither is
        # recoverable from the other
        workspace = done(tmp_path, TASK, OTHER)
        configured(workspace, tmp_path / "store")
        flaky_copy(monkeypatch)

        result = sign(workspace, publish=True)

        assert result.published is not None
        assert result.published.count == 1
        assert len(result.published.failed) == 1
        assert any("1 of 2 logs reached" in one for one in result.warnings)

    def test_and_journals_the_half_that_did(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # it journalled nothing at all, so what had actually landed in a shared
        # store existed in no record anywhere
        workspace = done(tmp_path, TASK, OTHER)
        configured(workspace, tmp_path / "store")
        flaky_copy(monkeypatch)

        sign(workspace, publish=True)

        event = published_event(workspace)
        assert event is not None
        assert event["logs"] == 1
        assert event["failed"] == 1

    def test_a_store_that_will_not_take_them_leaves_the_signature_standing(
        self, tmp_path: Path
    ) -> None:
        # **curation's rule exactly.** The signature is the person's act, and a
        # filesystem that would not cooperate must not unmake a decision they
        # already made -- so it warns where a failed move warns, and it happens
        # after the `SIGNOFF` event so there is nothing to unmake
        workspace = done(tmp_path, TASK)
        blocked = tmp_path / "store"
        blocked.write_text("not a directory", encoding="utf-8")
        configured(workspace, blocked)

        result = sign(workspace, publish=True)

        assert result.signature is not None
        assert result.published is None
        assert any("nothing was published" in one for one in result.warnings)
