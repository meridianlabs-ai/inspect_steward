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

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from inspect_steward._evalset.cache import read_attempt_cache
from inspect_steward._evalset.manifest import (
    Manifest,
    definition_hash,
    write_manifest,
)
from inspect_steward._evalset.observe import observe_logs
from inspect_steward._tend import (
    OBSERVATION,
    Move,
    Owner,
    Refused,
    TendError,
    TendResult,
    TuningPlan,
    Verdict,
    status,
    tend,
)
from inspect_steward._worker import ConfigView, resolve_inflight
from inspect_steward._workspace import (
    ACTION,
    PAUSED,
    RESUMED,
    Claim,
    DirectivesError,
    Held,
    Workspace,
    acquire,
    append_event,
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

    # and each departure is in the history, which is the only place a reader
    # arriving in the morning can learn that a task died twice in the night —
    # by then the snapshot shows one stalled task and no sign of how it got there
    written = workspace.status.read_text(encoding="utf-8")
    assert written.count("with probe") == 2
    # the promise of a retry is made only by the turn that made one: the second
    # departure is the one the guard gives up on, and telling a reader it is
    # being tried again would send them looking for a worker that never starts
    assert written.count("unfinished; it is being tried again") == 1


def test_a_worker_that_finished_its_task_is_not_history(tmp_path: Path) -> None:
    """A worker exits at the end of every task, so reaping is not news.

    Recording every reap would put a line in *what happened* for each task that
    completed, which is the run happening rather than something that happened
    to the run — and would drown the entries that are.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    turn(workspace)

    written = workspace.status.read_text(encoding="utf-8")
    assert "a worker exited" not in written


def test_a_worker_leaving_orphaned_work_behind_is_not_unfinished_work(
    tmp_path: Path,
) -> None:
    """An orphan is not *incomplete*; the manifest stopped asking for it.

    Every non-complete state used to count as work left undone, so a worker of
    a removed task departing was reported as something being picked up again —
    while the same turn archived its log and nothing ever respawned it. What a
    reader is owed there is the archive line, which says the true thing.
    """
    kept, removed = SynthTask("kept"), SynthTask("removed")
    workspace, _ = prepared(tmp_path, [kept])
    write_log(workspace.logs, kept)
    write_log(workspace.logs, removed, status="started", total=10, completed=3)

    turn(workspace)

    written = workspace.status.read_text(encoding="utf-8")
    assert (
        "removed"
        not in written.partition("## what happened")[2].partition("archived")[0]
    )
    assert "tried again" not in written


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


def test_the_status_a_turn_writes_says_it_was_tended_just_now(tmp_path: Path) -> None:
    """The turn writing the file *is* the tend, so the recorded age is the last one.

    `As of <now>` and `tended 10m ago` on the same line is one of them wrong,
    and on a first turn the age vanished entirely for a run being tended as the
    reader looked. A `status` renders the recorded age, which is right there
    for exactly the same reason.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    turn(workspace)
    first = workspace.status.read_text(encoding="utf-8")
    turn(workspace)
    second = workspace.status.read_text(encoding="utf-8")

    assert "tended just now" in first
    assert "tended just now" in second
    assert "ago" not in second.partition("\n\n##")[0]


def test_the_status_a_turn_writes_reports_what_that_turn_did(tmp_path: Path) -> None:
    """A summary must not contradict its own side effects.

    *What happened* is projected from a journal read taken before the actions
    run, so a turn that archives something wrote `status.md` saying nothing had
    ever been done to the run — and the entry surfaced only when some later
    turn happened to read the file again.
    """
    removed = SynthTask("removed")
    kept = SynthTask("kept")
    workspace, _ = prepared(tmp_path, [kept])
    write_log(workspace.logs, kept)
    write_log(workspace.logs, removed)

    turn(workspace)

    written = workspace.status.read_text(encoding="utf-8")
    assert "archived a log — orphaned" in written
    assert "Nothing has been done to this run yet" not in written


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


def test_a_tend_reads_the_directory_the_launch_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case the recorded field exists for, and it is the 02:00 one.

    A `log_root` arrives in the environment, and a scheduled tend inherits
    almost none of one. A turn that resolved this for itself would read the
    workspace's own `logs/` while the fleet wrote under the root — every task
    landing and then reading as never started, all night, with nothing saying
    why.
    """
    done = SynthTask("done")
    workspace, manifest = prepared(tmp_path, [done])
    under_a_root = tmp_path / "runs" / workspace.root.name
    write_manifest(
        manifest.model_copy(update={"log_dir": str(under_a_root)}), workspace.manifest
    )
    write_log(under_a_root, done)
    monkeypatch.delenv("STEWARD_LOG_ROOT", raising=False)

    result = turn(workspace)

    assert result.summary.states["complete"] == 1
    assert not workspace.logs.exists()
    # and the summary says where, because nothing else does: the runbook sends
    # an agent to `samples_df`, and there is no `logs/` here to guess at
    assert result.log_dir == str(under_a_root)
    assert f"**Logs** `{under_a_root}`" in workspace.status.read_text(encoding="utf-8")


def test_a_manifest_committed_before_the_field_resolves_as_it_always_did(
    tmp_path: Path,
) -> None:
    # absence means *resolve it the way it was resolved then*, which is without
    # a root, since there were none
    done = SynthTask("done")
    workspace, manifest = prepared(tmp_path, [done])
    assert manifest.log_dir is None
    write_log(workspace.logs, done)

    assert turn(workspace).summary.states["complete"] == 1


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
    workspace.directives.write_text("max_workers: 3\n", encoding="utf-8")
    turn(workspace)

    workspace.directives.write_text("max_workers: yes\n", encoding="utf-8")
    degraded = turn(workspace)

    assert degraded.degraded is not None
    assert "not True" in degraded.degraded
    # the settings the last good turn recorded, not Steward's own defaults --
    # running on those would silently discard what the operator wrote
    assert degraded.summary.max_workers == 3
    assert "_steward.yaml" in workspace.log.read_text(encoding="utf-8")


def test_a_broken_variable_keeps_the_files_standing_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file parsed; only the environment did not.

    Reported as one condition, a bad `INSPECT_EVAL_*` took the file's policies
    down with it — rules that had parsed perfectly, in the one place an agent
    is told to read them from.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    workspace.directives.write_text(
        "max_workers: 3\npolicies:\n  - never exceed ten dollars\n", encoding="utf-8"
    )
    turn(workspace)

    monkeypatch.setenv("INSPECT_EVAL_MAX_SAMPLES", "lots")
    degraded = turn(workspace)

    assert degraded.degraded is not None
    assert "INSPECT_EVAL_MAX_SAMPLES" in degraded.degraded
    assert degraded.policies == ["never exceed ten dollars"]


def test_two_different_broken_variables_are_two_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acknowledging one must not silence the other.

    Both were keyed on `_steward.yaml`'s modification time — the same file,
    unedited — so two unrelated failures shared an identity and the second
    arrived already accepted.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    workspace.directives.write_text("max_workers: 3\n", encoding="utf-8")
    turn(workspace)

    monkeypatch.setenv("INSPECT_EVAL_MAX_SAMPLES", "lots")
    first = turn(workspace)
    monkeypatch.delenv("INSPECT_EVAL_MAX_SAMPLES")
    monkeypatch.setenv("INSPECT_EVAL_MAX_TASKS", "heaps")
    second = turn(workspace)

    assert first.degraded_at is not None and second.degraded_at is not None
    assert first.degraded_at != second.degraded_at
    # and the same failure twice is one item rather than one per turn
    assert turn(workspace).degraded_at == second.degraded_at


def test_a_broken_steward_md_with_no_history_refuses(tmp_path: Path) -> None:
    # nothing to fall back to, and defaults would discard the operator's
    # instruction silently, which is the one outcome worse than stopping
    workspace, _ = prepared(tmp_path, [SynthTask("done")])
    workspace.directives.write_text("max_workers: nope\n", encoding="utf-8")

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


def test_a_tend_leaves_behind_what_the_next_one_can_skip(tmp_path: Path) -> None:
    done, other = SynthTask("done"), SynthTask("other")
    workspace, _ = prepared(tmp_path, [done, other])
    write_log(workspace.logs, done)
    write_log(workspace.logs, other)

    turn(workspace)
    cached = read_attempt_cache(workspace.observed)

    assert len(cached.entries) == 2
    # and the turn after it reads the same run out of them
    reused = turn(workspace)
    assert reused.summary.states["complete"] == 2


def test_the_cache_is_narrowed_to_the_directory_it_describes(tmp_path: Path) -> None:
    # bounded without a policy: an archived log leaves the directory, so the
    # turn that archives it also stops remembering it
    kept, removed = SynthTask("kept"), SynthTask("removed")
    workspace, _ = prepared(tmp_path, [kept])
    write_log(workspace.logs, kept)
    write_log(workspace.logs, removed)

    turn(workspace)

    entries = read_attempt_cache(workspace.observed).entries
    assert len(entries) == 1
    assert all("kept" in location for location in entries)


def test_a_discarded_cache_changes_nothing_but_the_time(tmp_path: Path) -> None:
    """Deleting the cache is invisible in the answer.

    Which is the property that makes it an accelerator rather than a second source of truth.
    """
    done, short = SynthTask("done"), SynthTask("short")
    workspace, _ = prepared(tmp_path, [done, short])
    write_log(workspace.logs, done)
    write_log(workspace.logs, short, total=4, completed=4)

    warm = status(workspace)
    workspace.observed.unlink(missing_ok=True)
    cold = status(workspace)

    assert warm.summary == cold.summary


def test_status_does_not_write_the_cache_either(tmp_path: Path) -> None:
    # it reads one gladly -- that is most of why it is cheap -- but *writes
    # nothing* is a promise worth being able to make without a footnote, and the
    # tend on the timer keeps the cache warm for it anyway
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    status(workspace)

    assert not workspace.observed.exists()


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    # sample concurrency is inspect's word, so it arrives as inspect's variable
    # scoped to this tool rather than as a flag Steward minted for it
    monkeypatch.setenv("STEWARD_MAX_SAMPLES", "7")
    turn(workspace, max_workers=4)

    (recorded,) = observations(workspace)
    assert recorded["settings"] == {
        "max_workers": 4,
        "max_tasks": None,
        "max_samples": 7,
        "samples_ramp": None,
        "stall_after": 2,
        "stuck_after": None,
    }
    assert recorded["states"]["complete"] == 1
    assert recorded["drift"] is False


def test_a_sample_pin_outlives_the_turn_that_set_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # unlike the pool settings, max_samples decides a regime rather than a
    # quantity: a pin that lapsed would leave the next tend ramping a level
    # nobody authorized and spawning the queue at the ramp's floor. And the
    # source is a shell variable, which the 02:00 tend does not inherit -- so
    # without the journal read-back the pin would last exactly one turn
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    monkeypatch.setenv("STEWARD_MAX_SAMPLES", "7")
    turn(workspace, max_workers=4)
    monkeypatch.delenv("STEWARD_MAX_SAMPLES")
    turn(workspace)

    _, second = observations(workspace)
    assert second["settings"]["max_samples"] == 7
    # the pool settings do lapse, which is the contrast that makes the pin one
    assert second["settings"]["max_workers"] is None


def test_a_samples_ramp_range_releases_a_recorded_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the way back: `_steward.yaml` holds standing wishes, and a range there is
    # the one instruction that cannot mean anything but *ramp this run*
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    monkeypatch.setenv("STEWARD_MAX_SAMPLES", "7")
    turn(workspace)
    monkeypatch.delenv("STEWARD_MAX_SAMPLES")
    workspace.directives.write_text("samples_ramp: [40, 100]\n")
    turn(workspace)

    _, second = observations(workspace)
    assert second["settings"]["max_samples"] is None
    assert second["settings"]["samples_ramp"] == [40, 100]


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


def test_a_finished_run_asks_to_be_accepted_rather_than_reading_as_all_clear(
    tmp_path: Path,
) -> None:
    """The one thing a completed sweep is owed, which it used to report as nothing.

    Every task done and no caveats used to render *Nothing needs attention.* —
    true of the machinery and false of the run, since the results exist and
    nobody has looked at them. Worded as a state because `steward signoff` is
    step 26; the item is acknowledgeable meanwhile.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    turn(workspace)

    rendered = workspace.status.read_text(encoding="utf-8")
    assert "waiting to be accepted" in rendered
    assert "Nothing needs attention." not in rendered


def test_status_md_is_quiet_when_there_is_nothing_to_say(tmp_path: Path) -> None:
    # a run with work still to do and nothing wrong with it -- the state that
    # genuinely owes a reader nothing
    workspace, _ = prepared(tmp_path, [SynthTask("pending")])

    turn(workspace)

    assert "Nothing needs attention." in workspace.status.read_text(encoding="utf-8")


# --- pausing ------------------------------------------------------------
#
# `reconcile` has taken `paused` since step 5 and nothing set it until the timer
# existed: before an armed timer, *not tending* was pausing. These are the turn
# honouring the flag, which is the whole of what pausing means.


def paused(workspace: Workspace, reason: str = "hold everything") -> None:
    append_event(workspace.journal, PAUSED, by="human", reason=reason)


def test_a_paused_run_schedules_nothing(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")])
    paused(workspace)

    result = turn(workspace)

    assert result.spawned == []
    assert result.summary.spawning == 0
    assert result.verdict is Verdict.PAUSED


def test_a_paused_run_moves_nothing_either(tmp_path: Path) -> None:
    # a paused run makes no changes to itself, and archiving a superseded log
    # is a change
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    write_log(workspace.logs, SynthTask("removed"))
    paused(workspace)

    assert turn(workspace).archived == []


def test_a_paused_run_still_records_what_it_saw(tmp_path: Path) -> None:
    # the series is what an agent reads the run as, and a night of silence in
    # it is indistinguishable from a night of nothing happening
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")])
    paused(workspace)

    turn(workspace)

    assert len(observations(workspace)) == 1


def test_resuming_schedules_again(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")], definition=EMPTY)
    paused(workspace)
    assert turn(workspace, max_workers=1).spawned == []

    append_event(workspace.journal, RESUMED)

    assert len(turn(workspace, max_workers=1).spawned) == 1


def test_status_previews_a_paused_run_as_paused(tmp_path: Path) -> None:
    # the preview has to describe what the next tend does, and what it does is
    # nothing
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")])
    paused(workspace)

    result = status(workspace)

    assert result.summary.spawning == 0
    assert result.verdict is Verdict.PAUSED


def test_a_pause_survives_a_deleted_state_directory(tmp_path: Path) -> None:
    """Why the flag is a journal event rather than a file under `.steward/`.

    That directory is documented as safe to delete. A pause living there would
    mean clearing a cache silently resumes an expensive run — and between the
    two directions this can fail in, a pause that outlives a wiped cache is
    recoverable and a resume nobody asked for is not.
    """
    workspace, _ = prepared(tmp_path, [SynthTask("waiting")])
    paused(workspace)
    turn(workspace)

    shutil.rmtree(workspace.state)
    prepared(tmp_path, [SynthTask("waiting")])

    assert turn(workspace).verdict is Verdict.PAUSED


# --- propagating the workspace ------------------------------------------


def test_a_turn_leaves_the_workspace_beside_its_logs(tmp_path: Path) -> None:
    """What the propagation is for, at the layer that performs it.

    A run's results go one place and everything explaining them goes another,
    and on a machine reachable only through an object store the second place is
    unreachable. So each turn mirrors the workspace into the log directory —
    always, rather than only when that directory is remote, since a mounted NAS
    has exactly the same need.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    turn(workspace)

    landed = {path.name for path in workspace.logs.iterdir()}
    assert {"status.md", "journal.jsonl"} <= landed
    # and the run's own results are still the only *logs* in there, which is
    # what the refusal in `_carried` protects
    assert observe_logs(workspace.logs).count == 1


def test_a_workspace_can_decline_to_propagate(tmp_path: Path) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    workspace.directives.write_text("sync: false\n", encoding="utf-8")

    turn(workspace)

    assert "status.md" not in {path.name for path in workspace.logs.iterdir()}


def test_the_flag_outranks_the_file(tmp_path: Path) -> None:
    # the third spelling, resolving the way every other setting does
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    workspace.directives.write_text("sync: false\n", encoding="utf-8")
    elsewhere = tmp_path / "watched"

    turn(workspace, sync=str(elsewhere))

    assert "status.md" in {path.name for path in elsewhere.iterdir()}


def test_a_status_propagates_nothing(tmp_path: Path) -> None:
    # `status` writes nothing at all, and a read verb that quietly pushed a
    # workspace to a bucket would be the surprise it promises not to be
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    status(workspace)

    assert "status.md" not in {path.name for path in workspace.logs.iterdir()}


# --- the machinery failing ------------------------------------------------
#
# A run's failures become items; the machinery's own failures used to become
# nothing at all -- a silent un-pause, a frozen snapshot, a kill loop with no
# witness. Each of these is one of those conditions getting a voice.


def edges(workspace: Workspace, action: str) -> list[dict[str, Any]]:
    """Every journalled episode edge of one kind, in file order."""
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == ACTION and event.payload.get("action") == action
    ]


def of_kind(result: TendResult, kind: str) -> list[Any]:
    return [item for item in result.items if item.kind == kind]


def test_an_unreadable_journal_refuses_the_turn(tmp_path: Path) -> None:
    """Proceeding on an empty history is worse than stopping.

    The journal holds the pause, the acknowledgments, and the last known
    settings — a turn that shrugged past it would silently un-pause an
    expensive run and re-open every accepted decision, which is exactly the
    kind of surprise the refusal costs ten minutes to avoid.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    turn(workspace)

    workspace.journal.chmod(0o000)
    try:
        with pytest.raises(TendError, match="pause"):
            tend(workspace)
    finally:
        workspace.journal.chmod(0o644)

    # and once it reads again, the run is simply the run
    assert isinstance(tend(workspace), TendResult)


def test_a_torn_journal_line_becomes_the_agents_item(tmp_path: Path) -> None:
    """Damage is kept rather than skipped in silence.

    Every fold the turn ran — the pause, the acks, the diff baseline — ran
    without whatever the torn line said, and the repair (reading the fragment,
    re-journalling what it meant) is judgement, so the item is the agent's.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    turn(workspace)
    with workspace.journal.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-08-23T10:00:00Z", "type": "obser\n')

    result = turn(workspace)

    (item,) = of_kind(result, "journal_damage")
    assert item.owner is Owner.AGENT
    assert "could not be read" in item.summary
    # the same tear is the same item, already announced and not again
    again = turn(workspace)
    assert [item.id] == [one.id for one in of_kind(again, "journal_damage")]
    assert item.id not in again.appeared


def test_status_md_unwritable_is_an_episode_with_edges(tmp_path: Path) -> None:
    """The failure whose only natural signal is a file going quietly stale.

    The first failing turn journals the episode's opening edge; the item fires
    from the journal on the turn after; the first working write closes it.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    turn(workspace)

    workspace.status.unlink()
    workspace.status.mkdir()  # every rename onto it now fails
    first = turn(workspace)
    assert of_kind(first, "status_unwritable") == []
    second = turn(workspace)
    (item,) = of_kind(second, "status_unwritable")
    assert "status.md" in item.summary
    # the edge was written once, not once per failing turn
    assert len(edges(workspace, "status_unwritable")) == 1

    workspace.status.rmdir()
    turn(workspace)  # writes again, closing the episode
    cleared = turn(workspace)
    assert of_kind(cleared, "status_unwritable") == []
    assert len(edges(workspace, "status_unwritable_restored")) == 1
    assert "could not write status.md" in workspace.log.read_text(encoding="utf-8")


def test_a_turn_reports_which_documents_it_actually_wrote(tmp_path: Path) -> None:
    """The only honest answer to *did this land*, for the one caller who needs it.

    A failed write never fails a turn — the turn already happened — so a turn
    that returns proves nothing about the files, and their existence proves
    less: a stale document from an earlier turn exists. `signoff` disarms the
    timer straight after its final turn, so nothing will come along to repair
    what that turn could not write, and it has to be told.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    assert turn(workspace).rendered == ["status.md", "anomalies.md"]

    workspace.status.unlink()
    workspace.status.mkdir()  # every rename onto it now fails

    assert turn(workspace).rendered == ["anomalies.md"]


def test_the_other_rendered_file_writing_does_not_close_the_episode(
    tmp_path: Path,
) -> None:
    """A persistent failure must not be rendered as a flapping one.

    `anomalies.md` is written beside `status.md` on the same terms, and both
    fail together for every cause anybody has. But where only the status is
    unwritable, the other file's success was recording a *restoration* — so the
    item vanished, the next turn opened the episode again, and one failure that
    never stopped arrived as news every second turn.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    turn(workspace)

    workspace.status.unlink()
    workspace.status.mkdir()  # every rename onto it now fails
    turn(workspace)
    turn(workspace)
    turn(workspace)
    result = turn(workspace)

    assert workspace.anomalies.exists(), "the premise: the other file still writes"
    assert of_kind(result, "status_unwritable"), "one episode, still open"
    assert edges(workspace, "status_unwritable") == [{"action": "status_unwritable"}]
    assert edges(workspace, "status_unwritable_restored") == []


def test_a_fresh_sync_failure_is_an_episode_but_not_yet_an_item(
    tmp_path: Path,
) -> None:
    # the propagation already retries every turn, so the episode worth a
    # person's attention is the one that outlives a retry -- a single slow
    # bucket write pages nobody
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    blocked = tmp_path / "bucket"
    blocked.write_text("a file where a directory must go", encoding="utf-8")
    workspace.directives.write_text(f"sync: {blocked}\n", encoding="utf-8")

    turn(workspace)
    fresh = turn(workspace)

    assert of_kind(fresh, "sync_failed") == []
    # one opening edge, however many turns keep failing
    assert len(edges(workspace, "sync_failed")) == 1


def test_a_sync_episode_that_outlives_an_interval_is_an_item_until_it_recovers(
    tmp_path: Path,
) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    healthy = turn(workspace)
    assert healthy.log_dir is not None
    # an episode that opened long ago, for the very destination this turn syncs
    with workspace.journal.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2020-01-01T00:00:00.000Z",
                    "type": "action",
                    "action": "sync_failed",
                    "target": healthy.log_dir,
                }
            )
            + "\n"
        )

    failing = turn(workspace)

    (item,) = of_kind(failing, "sync_failed")
    assert item.owner is Owner.HUMAN
    assert healthy.log_dir in item.summary
    # that same turn synced successfully, which closes the episode
    recovered = turn(workspace)
    assert of_kind(recovered, "sync_failed") == []
    assert item.id in recovered.resolved


def test_a_sync_episode_for_a_dropped_target_closes_itself(tmp_path: Path) -> None:
    # target A failing, then the configuration moves on -- sync pointed
    # elsewhere, or turned off entirely: nothing will ever write A's restore,
    # so the turn closes the episode rather than leaving a permanent warning
    # about a destination nobody configured anymore
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    turn(workspace)
    with workspace.journal.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2020-01-01T00:00:00.000Z",
                    "type": "action",
                    "action": "sync_failed",
                    "target": str(tmp_path / "old-bucket"),
                }
            )
            + "\n"
        )
    workspace.directives.write_text("sync: false\n", encoding="utf-8")

    turn(workspace)
    cleared = turn(workspace)

    assert of_kind(cleared, "sync_failed") == []
    assert len(edges(workspace, "sync_restored")) == 1


def broke_a_wedge(workspace: Workspace) -> TendResult:
    """One turn that had to kill a wedged predecessor to take the claim.

    Through `tend(claim=...)`, which is the path a real break takes once
    `acquire` has done the killing — arranging an actually wedged process is
    `test_claim.py`'s subject, and what this file owes is what the turn does
    with the fact.
    """
    outcome = acquire(workspace.claim, command="tend")
    assert isinstance(outcome, Claim)
    outcome.broke = Held(pid=4242, host="here", command="tend", since=None, stale=True)
    with outcome:
        return turn(workspace, claim=outcome)


def test_one_wedge_break_is_recovery_and_two_in_a_row_are_a_kill_loop(
    tmp_path: Path,
) -> None:
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)

    first = broke_a_wedge(workspace)

    # one break is the recovery working as designed: journalled, not an item
    assert of_kind(first, "kill_loop") == []
    (edge,) = edges(workspace, "claim_broke")
    assert edge.get("pid") == 4242

    second = broke_a_wedge(workspace)

    (item,) = of_kind(second, "kill_loop")
    assert item.owner is Owner.HUMAN
    assert "2 in a row" in item.summary


def test_a_loop_of_crashed_breakers_is_seen_and_clears_after_clean_turns(
    tmp_path: Path,
) -> None:
    """The loop's worst shape leaves only the breaks behind.

    A tend that breaks its predecessor and then wedges itself never reaches an
    observation, so the chain is counted per break — and what ends it is the
    one thing a loop cannot produce, an observation from a turn that broke
    nothing. The evidence sits behind one clean observation for one turn (the
    fold cannot tell a crashed breaker from a breaker that completed), so the
    item outlives the loop by exactly one turn.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    append_event(workspace.journal, ACTION, action="claim_broke", pid=111)
    append_event(workspace.journal, ACTION, action="claim_broke", pid=222)

    looping = turn(workspace)
    (item,) = of_kind(looping, "kill_loop")
    lingering = turn(workspace)
    assert [item.id] == [one.id for one in of_kind(lingering, "kill_loop")]
    cleared = turn(workspace)
    assert of_kind(cleared, "kill_loop") == []


def moved(task: SynthTask) -> TuningPlan:
    """A plan holding one retune, for wiring straight into a turn."""
    return TuningPlan(
        active=True,
        range=(40, 200),
        moves=[
            Move(
                identifier=task.identifier,
                key=task.name,
                task_id="T1",
                knob="max_samples",
                to=60,
                at=40,
                reason="a clean window",
            )
        ],
    )


def test_a_retune_the_cli_refuses_costs_the_move_and_not_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A usage error is a defect here, and it must not fail the tend.

    Unwrapped, it did: the whole turn died on it — no observation, no
    snapshot, no post — every interval, with nothing saying so anywhere but a
    traceback nobody was standing at.
    """
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    plan = moved(done)

    def planned(*args: Any, **kwargs: Any) -> TuningPlan:
        return plan

    monkeypatch.setattr("inspect_steward._tend.turn.plan_tuning", planned)

    def refuses(*args: Any, **kwargs: Any) -> ConfigView:
        raise RuntimeError("ctl: unrecognized option")

    monkeypatch.setattr("inspect_steward._tend.turn.task_config", refuses)

    result = turn(workspace)

    (failure,) = [f for f in result.failures if "could not retune done" in f]
    assert "RuntimeError" in failure
    assert any(item.kind == "action_failed" for item in result.items)
    assert "could not retune done" in workspace.log.read_text(encoding="utf-8")
    # the turn itself happened: observed, recorded, written
    assert workspace.status.exists()
    assert len(observations(workspace)) == 1


def test_a_retune_that_was_not_filed_is_logged_as_well_as_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the failures list feeds one turn's items; `steward.log` is where a
    # reader reconstructs the machinery's night -- the other paths in the loop
    # already wrote both, and this one silently wrote only the first
    done = SynthTask("done")
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    plan = moved(done)

    def planned(*args: Any, **kwargs: Any) -> TuningPlan:
        return plan

    def files_nothing(*args: Any, **kwargs: Any) -> ConfigView:
        return ConfigView(applied=True, persisted={"max_samples": False})

    monkeypatch.setattr("inspect_steward._tend.turn.plan_tuning", planned)
    monkeypatch.setattr("inspect_steward._tend.turn.task_config", files_nothing)

    result = turn(workspace)

    assert any("was not recorded in the eval log" in f for f in result.failures)
    assert "was not recorded in the eval log" in workspace.log.read_text(
        encoding="utf-8"
    )


def test_status_md_gains_an_errored_column_only_where_something_errored(
    tmp_path: Path,
) -> None:
    flaky, clean = SynthTask("flaky"), SynthTask("clean")
    workspace, _ = prepared(tmp_path / "errored", [flaky, clean])
    write_log(workspace.logs, flaky, total=10, completed=7)
    write_log(workspace.logs, clean, total=10, completed=10)

    turn(workspace)

    written = workspace.status.read_text(encoding="utf-8")
    assert "| errored |" in written
    assert "| 3 |" in written

    quiet, _ = prepared(tmp_path / "quiet", [clean])
    write_log(quiet.logs, clean, total=10, completed=10)

    turn(quiet)

    assert "| errored |" not in quiet.status.read_text(encoding="utf-8")
