"""What a rehearsal tells the launch that follows it, and what the launch does about it.

Coverage is **two** questions, and the second is the one that would be easy to leave out. Task identifiers say which tasks were rehearsed and are the half that can name a number; they are deliberately blind to how much of each task runs, so a dataset that doubled keeps every identifier while being a different night's work. The manifest digest is what notices that, and it is comparable at all only because a rehearsal's slice rides its workers rather than its capture.

And an uncovered launch **warns and proceeds**: re-launching after a fix and resuming an interrupted run are both legitimate reasons to have no current rehearsal, and a gate that refused would be one people learn to route around.
"""

from pathlib import Path

import pytest
from inspect_steward._evalset.manifest import Manifest, manifest_digest
from inspect_steward._launch import Launch, launch
from inspect_steward._launch.launch import _unrehearsed
from inspect_steward._scan import merged_scanners, scan_digest, scan_material
from inspect_steward._smoke import Outcome
from inspect_steward._smoke.digest import Smoke, journal_fields
from inspect_steward._workspace import (
    SMOKED,
    Workspace,
    append_event,
    create_workspace,
    read_journal,
    read_smoked,
)

from .._logs import SynthTask, synth_manifest
from ..launch._fake import fake_capture
from ..timer._fake import clear_credentials, fake_cron

ADDITION = SynthTask("addition", samples=2)
ECHO = SynthTask("echo", samples=1)


BUILT_IN = scan_digest(scan_material(None, None))
"""What a launch merges in when nothing else scans — the configuration every rehearsal of an unadorned definition runs under."""


def rehearsed(
    workspace: Workspace,
    *tasks: SynthTask,
    verdict: Outcome = Outcome.PASSED,
    manifest: Manifest | None = None,
    scanners: str = BUILT_IN,
    scan_model: str = "",
) -> None:
    """Journal a smoke that covered exactly `tasks`."""
    smoke = Smoke(
        outcome=verdict,
        identifiers=tuple(task.identifier for task in tasks),
        digest=manifest_digest(manifest or synth_manifest(list(tasks))),
        scanners=scanners,
        scan_model=scan_model,
        log_dir=str(workspace.smoke),
    )
    append_event(workspace.journal, SMOKED, **journal_fields(smoke))


def launched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *tasks: SynthTask,
    manifest: Manifest | None = None,
) -> Launch:
    """Capture `tasks` and launch, without a timer and without a subprocess."""
    fake_cron(monkeypatch)
    clear_credentials(monkeypatch)
    workspace = Workspace.at(tmp_path)
    definition = tmp_path / "evalset.py"
    fake_capture(monkeypatch, manifest or synth_manifest(list(tasks)))
    result = launch(workspace, definition, timer=False)
    assert isinstance(result, Launch)
    return result


class TestWhatTheJournalRemembers:
    """`read_smoked` — the fold a launch consults."""

    def test_a_workspace_with_no_smoke_has_covered_nothing(
        self, tmp_path: Path
    ) -> None:
        create_workspace(tmp_path, git=False)

        rehearsal = read_smoked(read_journal(Workspace.at(tmp_path).journal).events)

        assert rehearsal.identifiers == frozenset()
        assert rehearsal.digest is None

    def test_only_a_passing_smoke_counts_as_coverage(self, tmp_path: Path) -> None:
        # a failed rehearsal is a record worth keeping and not a claim about
        # what was rehearsed
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        rehearsed(workspace, ADDITION, verdict=Outcome.FAILED)

        assert read_smoked(read_journal(workspace.journal).events).identifiers == (
            frozenset()
        )

    def test_the_most_recent_pass_wins(self, tmp_path: Path) -> None:
        # an older pass describes a definition that has since been rehearsed
        # again, so the later run is the one that describes the current one
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        rehearsed(workspace, ADDITION, ECHO)
        rehearsed(workspace, ADDITION)

        assert read_smoked(read_journal(workspace.journal).events).identifiers == (
            frozenset({ADDITION.identifier})
        )

    def test_a_later_failure_retires_an_earlier_pass(self, tmp_path: Path) -> None:
        # **the newest smoke is the answer, whatever it concluded.** Reading back
        # to the most recent *pass* would let a rehearsal that just failed sit
        # behind one from an hour ago and report the launch as rehearsed -- the
        # one reading a person would never make from the same journal
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        rehearsed(workspace, ADDITION, ECHO)
        rehearsed(workspace, ADDITION, ECHO, verdict=Outcome.FAILED)

        assert read_smoked(read_journal(workspace.journal).events).identifiers == (
            frozenset()
        )

    def test_a_record_written_without_a_digest_reads_as_cannot_say(
        self, tmp_path: Path
    ) -> None:
        # `None` rather than `""`, so a caller compares only where there is
        # something to compare and an older record is not called stale for
        # having been written by an earlier version
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        append_event(
            workspace.journal,
            SMOKED,
            identifiers=[ADDITION.identifier],
            verdict="passed",
        )

        rehearsal = read_smoked(read_journal(workspace.journal).events)

        assert rehearsal.identifiers == frozenset({ADDITION.identifier})
        assert rehearsal.digest is None


class TestWhatTheLaunchDoesAboutIt:
    """The warning, and the fact that it is only ever a warning."""

    def test_a_launch_with_no_smoke_is_warned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        create_workspace(tmp_path, git=False)

        result = launched(tmp_path, monkeypatch, ADDITION, ECHO)

        assert result.unrehearsed == "no smoke has passed for this workspace"
        # and it commits anyway, which is the whole of the design decision here
        assert result.committed is True

    def test_a_launch_the_smoke_covers_is_not_warned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        create_workspace(tmp_path, git=False)
        rehearsed(Workspace.at(tmp_path), ADDITION, ECHO)

        assert launched(tmp_path, monkeypatch, ADDITION, ECHO).unrehearsed is None

    def test_a_task_added_since_the_smoke_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the case the check exists for: an edit added work nothing rehearsed
        create_workspace(tmp_path, git=False)
        rehearsed(Workspace.at(tmp_path), ADDITION)

        result = launched(tmp_path, monkeypatch, ADDITION, ECHO)

        assert result.unrehearsed is not None
        assert "does not cover 1 task" in result.unrehearsed
        assert result.committed is True

    def test_a_task_dropped_since_the_smoke_is_not_a_gap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # subset rather than equality: what matters is that nothing about to
        # run is unrehearsed, and a task that left rehearsed nothing to run.
        # The digest differs here for exactly that reason, which is why the
        # shape question is asked only of an equal set -- reporting it would
        # name the removal a second time, under a word that does not describe it
        create_workspace(tmp_path, git=False)
        rehearsed(Workspace.at(tmp_path), ADDITION, ECHO)

        assert launched(tmp_path, monkeypatch, ADDITION).unrehearsed is None

    def test_a_dataset_that_grew_since_the_smoke_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**Why identifiers alone are not the whole question.**

        `task_identifier` hashes execution limits and pointedly not the sample
        count or the epochs, so that raising either leaves existing logs
        resumable. A dataset that doubled therefore keeps every identifier while
        being a materially different night, which is the case the digest exists
        to notice.
        """
        create_workspace(tmp_path, git=False)
        grown = SynthTask("addition", samples=200)
        rehearsed(Workspace.at(tmp_path), ADDITION)
        assert grown.identifier == ADDITION.identifier

        result = launched(tmp_path, monkeypatch, grown)

        assert result.unrehearsed is not None
        assert "different shape" in result.unrehearsed
        assert result.committed is True

    def test_a_selection_that_changed_since_the_smoke_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `limit`, `sample_id` and `sample_shuffle` are identity-neutral *and*
        # count-neutral, so nothing but the digest sees them move
        create_workspace(tmp_path, git=False)
        rehearsed(
            Workspace.at(tmp_path),
            ADDITION,
            manifest=synth_manifest([ADDITION], limit=1),
        )

        result = launched(
            tmp_path, monkeypatch, manifest=synth_manifest([ADDITION], limit=2)
        )

        assert result.unrehearsed is not None
        assert "different shape" in result.unrehearsed

    def test_a_scan_configuration_changed_since_the_smoke_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `manifest_digest` hashes the tasks and the run's shaping and not
        # `Manifest.scan`, so nothing else in the record can see this: a scanner
        # added afterwards reviews every transcript having been exercised on none
        create_workspace(tmp_path, git=False)
        rehearsed(Workspace.at(tmp_path), ADDITION, scanners="sha256:something-else")

        result = launched(tmp_path, monkeypatch, ADDITION)

        assert result.unrehearsed is not None
        assert "different configuration" in result.unrehearsed
        assert result.committed is True

    def test_a_scanners_parameters_are_part_of_that(self) -> None:
        # **names are not the configuration.** A changed parameter, a different
        # scan-side model, or a filter narrowing which transcripts a scanner
        # sees all leave the names identical and change what the rows say
        one = scan_material(None, {"mine": {"name": "pkg/scanner", "params": {"k": 1}}})
        two = scan_material(None, {"mine": {"name": "pkg/scanner", "params": {"k": 2}}})

        assert sorted(merged_scanners(one)) == sorted(merged_scanners(two))
        assert scan_digest(one) != scan_digest(two)

    def test_a_scan_model_changed_since_the_smoke_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the rehearsal established a context window for one model and nothing
        # at all about another
        create_workspace(tmp_path, git=False)
        rehearsed(Workspace.at(tmp_path), ADDITION, scan_model="openai/gpt-5")

        result = launched(tmp_path, monkeypatch, ADDITION)

        assert (
            result.unrehearsed
            == "the last passing smoke scanned with a different model"
        )

    def test_a_record_that_predates_the_scan_fields_is_not_called_stale(
        self, tmp_path: Path
    ) -> None:
        # `""` is a real answer for `scan_model` -- *none was configured* -- so
        # only the sibling field can say whether anybody wrote either down
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        append_event(
            workspace.journal,
            SMOKED,
            identifiers=[ADDITION.identifier],
            verdict="passed",
        )

        rehearsal = read_smoked(read_journal(workspace.journal).events)

        assert rehearsal.scanners is None
        assert rehearsal.scan_model is None

    def test_the_slice_a_rehearsal_ran_under_is_not_a_shape_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**Why the digest is usable here at all.**

        A rehearsal truncates its *workers*, not its capture, so the manifest it
        records the digest of is the one the launch will capture. Applying the
        slice at capture — the obvious alternative — would move the digest on
        every smoke and report every launch as reshaped, always.
        """
        create_workspace(tmp_path, git=False)
        whole = synth_manifest([ADDITION, ECHO])
        truncated = synth_manifest([ADDITION, ECHO], limit=1)
        assert manifest_digest(truncated) != manifest_digest(whole)

        rehearsed(Workspace.at(tmp_path), ADDITION, ECHO, manifest=whole)

        assert launched(tmp_path, monkeypatch, manifest=whole).unrehearsed is None


class TestAJournalThatCannotBeRead:
    """Unknown coverage is a warning, not a silence.

    The check's whole output is advice, so warning when the answer cannot be established costs one line — and staying quiet asserts *rehearsed* on no evidence at all. Damage counts for a sharper reason than that: a torn line is what a crash mid-append leaves at the **end** of the file, which is exactly where the newest smoke is, so the fold reads the pass before it and reports coverage that has since been superseded.
    """

    def test_a_journal_that_will_not_read_warns(self, tmp_path: Path) -> None:
        # called directly: a journal this unreadable takes the whole launch down
        # a few steps later, and what is under test is that *this* answer is
        # `cannot say` rather than `nothing to say`
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        rehearsed(workspace, ADDITION)
        workspace.journal.chmod(0o000)
        try:
            warning = _unrehearsed(workspace, synth_manifest([ADDITION]), None)
        finally:
            workspace.journal.chmod(0o644)

        assert warning is not None
        assert "could not be read" in warning

    def test_a_torn_newest_record_does_not_leave_an_older_pass_standing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the crash-mid-append shape: a good pass, then a fragment of the smoke
        # that superseded it. Reading past the fragment reports the older answer
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        rehearsed(workspace, ADDITION)
        with workspace.journal.open("a", encoding="utf-8") as f:
            f.write('{"ts": "2026-09-01T00:00:00+00:00", "type": "smo\n')

        result = launched(tmp_path, monkeypatch, ADDITION)

        assert result.unrehearsed is not None
        assert "damaged" in result.unrehearsed
