"""The turn, against synthesized state.

Almost all of this is layer 1: a manifest committed by hand, a log directory
`tests/_logs.py` wrote without running anything, and a definition that is an
empty Python file. Where a process is genuinely the subject — a worker that
lands no log, so that only the in-flight record knows it was ever tried — the
definition really is executed, and it costs a bare interpreter rather than an
eval (~30ms), which is step 8's *when the process boundary is the subject but
the eval is not, do not launch an eval*.

The two claims the step is held to are the last two tests plus
`test_a_settled_run_is_a_no_op_and_stays_one`: a repeated tend is a no-op, and
a turn interrupted at any point is recovered by the following one.
"""

from pathlib import Path
from typing import Any

import pytest
from inspect_steward._evalset.manifest import (
    Manifest,
    definition_hash,
    write_manifest,
)
from inspect_steward._tend import (
    OBSERVATION,
    Refused,
    TendError,
    TendResult,
    status,
    tend,
)
from inspect_steward._worker import resolve_inflight
from inspect_steward._workspace import (
    Claim,
    DirectivesError,
    Workspace,
    acquire,
    read_journal,
)

from .._fault import until
from .._logs import DEFINITION, SynthTask, synth_manifest, write_log

EMPTY = b"# a definition that resolves nothing and exits\n"
"""A definition a worker can really run, cheaply, that lands no log."""


def prepared(
    root: Path,
    tasks: list[SynthTask],
    *,
    definition: bytes = EMPTY,
    **options: Any,
) -> tuple[Workspace, Manifest]:
    """A workspace with a definition and a manifest committed against it.

    The content hash is computed from the file rather than taken from
    `synth_manifest`, which is what a real capture does — so drift is `False`
    by construction and a test that edits the file flips it.
    """
    workspace = Workspace.at(root)
    workspace.root.mkdir(parents=True, exist_ok=True)
    path = workspace.root / DEFINITION
    path.write_bytes(definition)

    manifest = synth_manifest(tasks, **options)
    manifest = manifest.model_copy(
        update={
            "source": manifest.source.model_copy(
                update={"content_hash": definition_hash(path)}
            )
        }
    )
    write_manifest(manifest, workspace.manifest)
    return workspace, manifest


def settle(workspace: Workspace) -> None:
    """Wait until nothing this workspace launched is running."""
    until(
        "the workers to exit",
        lambda: not resolve_inflight(workspace.inflight, workspace.workers).running,
    )


def turn(workspace: Workspace, **kwargs: Any) -> TendResult:
    """A tend that is expected to run rather than be refused."""
    result = tend(workspace, **kwargs)
    assert isinstance(result, TendResult), f"refused by {result}"
    return result


def observations(workspace: Workspace) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == OBSERVATION
    ]


def test_a_settled_run_is_a_no_op_and_stays_one(tmp_path: Path) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    first = turn(workspace)
    second = turn(workspace)

    for result in (first, second):
        assert (result.spawned, result.reaped, result.archived) == ([], [], [])
        assert result.failures == []
        assert result.summary.states["complete"] == 1
    # one observation per turn, whether or not anything happened: the series is
    # what an arriving agent reads, and a quiet night is still a night
    assert len(observations(workspace)) == 2


def test_a_turn_does_not_depend_on_its_own_history(tmp_path: Path) -> None:
    """An interrupted turn is reconciled by the next one.

    The mechanism is that a turn derives everything from the log directory and
    the process table rather than from what it remembers writing.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    turn(workspace)

    # exactly what a turn interrupted between acting and recording leaves
    workspace.journal.unlink()
    workspace.status.unlink()

    recovered = turn(workspace)

    assert (recovered.spawned, recovered.reaped, recovered.archived) == ([], [], [])
    assert recovered.summary.states["complete"] == 1
    assert workspace.status.exists()


def test_a_worker_that_lands_no_log_is_tried_twice_and_then_left(
    tmp_path: Path,
) -> None:
    """The crash loop the log directory cannot see.

    A definition that will not import, or an OOM during startup, leaves nothing
    behind — so the task reads `missing` on every turn exactly as it did on the
    first. Only the in-flight record knows it was tried, and without the guard
    this respawns forever, invisibly.
    """
    probe = SynthTask("probe")
    workspace, manifest = prepared(tmp_path, [probe])

    first = turn(workspace)
    settle(workspace)
    second = turn(workspace)
    settle(workspace)
    third = turn(workspace)

    assert len(first.spawned) == 1
    # the second turn reaps the first attempt and, one failure being ordinary,
    # tries once more
    assert second.reaped == first.spawned
    assert len(second.spawned) == 1
    # the third has two spent attempts and nothing to show for either
    assert third.spawned == []
    assert third.summary.stalled == [probe.identifier]
    assert third.reaped == second.spawned
    assert manifest.tasks[0].identifier == probe.identifier


def test_an_orphan_is_archived_once_and_the_journal_says_why(tmp_path: Path) -> None:
    removed = SynthTask("removed")
    kept = SynthTask("kept")
    workspace, _ = prepared(tmp_path, [kept])
    write_log(workspace.logs, kept)
    landed = write_log(workspace.logs, removed)

    first = turn(workspace)
    second = turn(workspace)

    # moved, never deleted -- the invariant the whole archive exists for
    assert not landed.exists()
    archive = workspace.logs_archive / landed.name
    assert archive.exists()
    assert first.archived == [str(archive)]
    assert second.archived == []

    (action,) = [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == "action"
    ]
    assert action["action"] == "archive"
    assert action["reason"] == "orphaned"
    assert action["identifier"] == removed.identifier
    assert action["archived"] == str(archive)


def test_the_archive_is_a_sibling_of_whatever_log_dir_the_definition_chose(
    tmp_path: Path,
) -> None:
    # derived from log_dir rather than from the workspace, because a result's
    # archive belongs beside the result
    removed = SynthTask("removed")
    elsewhere = tmp_path / "somewhere" / "results"
    workspace, _ = prepared(tmp_path, [SynthTask("kept")], log_dir=str(elsewhere))
    write_log(elsewhere, removed)

    turn(workspace)

    assert (tmp_path / "somewhere" / "results-archive" / "").parent.exists()
    assert not workspace.logs_archive.exists()


def test_a_relative_log_dir_is_relative_to_the_workspace(tmp_path: Path) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done], log_dir="results")
    write_log(workspace.root / "results", done)

    result = turn(workspace)

    assert result.summary.states["complete"] == 1


def test_drift_is_reported_and_never_applied(tmp_path: Path) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    assert turn(workspace).drift is False

    (workspace.root / DEFINITION).write_bytes(EMPTY + b"# an edit nobody applied\n")
    drifted = turn(workspace)

    assert drifted.drift is True
    assert "steward launch" in workspace.status.read_text(encoding="utf-8")
    # reported, never acted on: the manifest is still the one that was committed
    assert drifted.summary.states["complete"] == 1
    assert drifted.spawned == []


def test_a_definition_that_has_gone_reads_as_drift(tmp_path: Path) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    (workspace.root / DEFINITION).unlink()

    assert turn(workspace).drift is True


def test_a_broken_steward_md_degrades_to_the_last_known_good(tmp_path: Path) -> None:
    """A typo does not stop a fleet converging.

    Somebody may edit this file at 10pm with twenty workers up, and a turn that
    refused over it would be the unattended failure the timer exists to prevent.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    workspace.directives.write_text("---\nmax_workers: 3\n---\n", encoding="utf-8")
    turn(workspace)

    workspace.directives.write_text("---\nmax_workers: yes\n---\n", encoding="utf-8")
    degraded = turn(workspace)

    assert degraded.degraded is not None
    assert "not True" in degraded.degraded
    # the settings the last good turn recorded, not Steward's own defaults --
    # running on those would silently discard what the operator wrote
    assert degraded.summary.max_workers == 3
    assert "_steward.md" in workspace.log.read_text(encoding="utf-8")


def test_a_broken_steward_md_with_no_history_refuses(tmp_path: Path) -> None:
    # nothing to fall back to, and defaults would discard the operator's
    # instruction silently, which is the one outcome worse than stopping
    workspace, _ = prepared(tmp_path, [SynthTask("done")])
    workspace.directives.write_text("---\nmax_workers: nope\n---\n", encoding="utf-8")

    with pytest.raises(DirectivesError):
        tend(workspace)


def test_status_writes_nothing_at_all(tmp_path: Path) -> None:
    """Read-only, as every convention in the ecosystem promises.

    Somebody typing this to satisfy their curiosity about an overnight sweep
    must not thereby launch eight workers.
    """
    probe = SynthTask("probe")
    workspace, _ = prepared(tmp_path, [probe])

    result = status(workspace)

    assert result.executed is False
    # it would spawn, and it did not
    assert result.summary.spawning == 1
    assert result.spawned == []
    assert not workspace.status.exists()
    assert not workspace.journal.exists()
    assert not workspace.claim.exists()
    assert not workspace.inflight.exists()


def test_status_previews_exactly_what_the_next_turn_does(tmp_path: Path) -> None:
    done, removed = SynthTask("done"), SynthTask("removed")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    write_log(workspace.logs, removed)

    preview = status(workspace)
    executed = turn(workspace)

    assert preview.summary.archiving == 1
    assert len(executed.archived) == preview.summary.archiving
    assert preview.summary.states == executed.summary.states


def test_a_tend_refuses_while_the_claim_is_held(tmp_path: Path) -> None:
    """Two turns at once is the ordinary case, not an exotic one.

    A timer fires while an agent is mid-tend, and a kernel lock catches the
    double-acquire within one process as readily as between two.
    """
    workspace, _ = prepared(tmp_path, [SynthTask("probe")])
    outcome = acquire(workspace.claim, command="tend")
    assert isinstance(outcome, Claim)

    with outcome:
        refused = tend(workspace)

    assert isinstance(refused, Refused)
    assert refused.held.command == "tend"
    # a refused turn does nothing at all -- not to the journal, not to the
    # snapshot, and not to the operational log either. A timer firing every ten
    # minutes against an agent's long-held claim writes nothing each time
    assert observations(workspace) == []
    assert not workspace.status.exists()
    assert not workspace.log.exists()

    # and the claim, once released, is simply free
    assert isinstance(tend(workspace), TendResult)


def test_status_reports_a_claim_rather_than_taking_one(tmp_path: Path) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    outcome = acquire(workspace.claim, command="tend")
    assert isinstance(outcome, Claim)

    with outcome:
        result = status(workspace)

    assert result.claim is not None
    assert result.claim.command == "tend"
    assert result.summary.states["complete"] == 1


def test_a_log_directory_that_cannot_be_read_stops_the_turn(tmp_path: Path) -> None:
    """Nothing is spawned into a directory that cannot be read.

    Scheduling work whose results have nowhere to land would multiply the loss,
    and the failure is recorded where machinery failures go, not in the journal.
    """
    workspace, _ = prepared(tmp_path, [SynthTask("probe")])
    workspace.logs.mkdir(parents=True)
    workspace.logs.chmod(0o000)
    try:
        with pytest.raises(TendError, match="could not be read"):
            tend(workspace)
    finally:
        workspace.logs.chmod(0o700)

    assert observations(workspace) == []
    assert "could not read the log directory" in workspace.log.read_text(
        encoding="utf-8"
    )


def test_no_committed_manifest_says_what_to_run(tmp_path: Path) -> None:
    workspace = Workspace.at(tmp_path)
    workspace.root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(TendError, match="steward launch"):
        tend(workspace)


def test_the_observation_carries_the_settings_a_later_turn_reads_back(
    tmp_path: Path,
) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    turn(workspace, max_workers=4, max_samples=7)

    (recorded,) = observations(workspace)
    assert recorded["settings"] == {
        "max_workers": 4,
        "max_samples": 7,
        "stall_after": 2,
    }
    assert recorded["states"]["complete"] == 1
    assert recorded["drift"] is False


def test_status_md_says_how_old_it_is_and_what_needs_a_person(
    tmp_path: Path,
) -> None:
    stuck, done = SynthTask("stuck"), SynthTask("done")
    workspace, _ = prepared(tmp_path, [stuck, done])
    write_log(workspace.logs, done)
    for hour, count in enumerate((4, 4, 4)):
        write_log(
            workspace.logs,
            stuck,
            total=count,
            completed=count,
            created=f"2026-08-23T{10 + hour:02d}:00:00+00:00",
        )

    turn(workspace)
    rendered = workspace.status.read_text(encoding="utf-8")

    # the age is a fact about the file rather than part of the snapshot: a
    # remote reader detects a stopped timer by noticing this stopped changing
    assert "**As of** `20" in rendered
    assert "stopped making progress" in rendered
    assert "| complete | 1 |" in rendered


def test_status_md_is_quiet_when_there_is_nothing_to_say(tmp_path: Path) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    turn(workspace)

    assert "Nothing needs attention." in workspace.status.read_text(encoding="utf-8")
