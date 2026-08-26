"""The one list a turn produces, and the verdict over it.

Layer 1 throughout, and deliberately driven through the real `tend`: an item is
a projection of what a turn saw, so constructing one by hand would test the
constructor. The conditions are synthesized in the workspace — a definition
edited after capture, a file that is not a log, a task that crashed twice — and
what is asserted is what came out the other end.

Two properties carry the design and get the most cases. **The id is the
re-notification policy**: acknowledging something must silence exactly it, and
must not silence the next one. And **the verdict is run-level**, so a finished
run with a caveat and a stuck run with the same caveat must not read alike.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from inspect_steward._tend import (
    OBSERVATION,
    Level,
    Owner,
    Verdict,
    status,
    verdict,
    verdict_line,
)
from inspect_steward._tend.items import (
    ACTION_FAILED,
    DEGRADED,
    DRIFT,
    STALLED,
    TIMER_DRIFT,
    UNREADABLE,
    UNSUPERVISED,
    Item,
)
from inspect_steward._workspace import (
    ACKNOWLEDGED,
    ARMED,
    DISARMED,
    Workspace,
    append_event,
)

from .._logs import DEFINITION, SynthTask, write_log
from .test_tend import prepared, turn

TASK = SynthTask("probe", samples=4)

STUCK = SynthTask("stuck")
"""Ten samples, so a log carrying four of them reads `short` rather than done."""


def items(workspace: Workspace) -> dict[str, Item]:
    """The items of one real turn, by kind."""
    return {item.kind: item for item in turn(workspace).items}


def ack(workspace: Workspace, identifier: str, *, by: str = "human") -> None:
    """Dispose of an item, as `steward ack` does."""
    append_event(
        workspace.journal, ACKNOWLEDGED, id=identifier, by=by, reason="because"
    )


def stalling(workspace: Workspace, attempts: int, *, first: int = 10) -> None:
    """Land `attempts` logs for `STUCK` that all got exactly as far as each other.

    The log-visible half of the stall guard, which needs no processes at all —
    the crash-loop half does, and this file is not about the guard. Four samples
    of the ten the manifest asks for, the same four every time.
    """
    for hour in range(attempts):
        write_log(
            workspace.logs,
            STUCK,
            total=4,
            completed=4,
            created=f"2026-08-23T{first + hour:02d}:00:00+00:00",
        )


# --- what a condition projects to ---------------------------------------


def test_a_definition_edited_after_capture_is_the_human_s(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.root / DEFINITION).write_bytes(b"# edited\n")

    item = items(workspace)[DRIFT]

    assert item.owner is Owner.HUMAN
    assert item.level is Level.ATTENTION
    assert item.action == "steward launch"
    assert item.acknowledgeable


def test_a_file_that_is_not_a_log_is_the_agent_s(tmp_path: Path) -> None:
    # nothing here needs a decision -- somebody has to go and look, which is
    # investigation rather than a question
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")

    item = items(workspace)[UNREADABLE]

    assert item.owner is Owner.AGENT
    assert item.id == "unreadable:broken.eval"
    # the location is an absolute URI; the line a person reads is not
    assert item.subject.endswith("broken.eval") and item.subject != item.id


def test_an_item_names_its_task_readably_and_pins_it_exactly(tmp_path: Path) -> None:
    # a task identifier is ~200 characters with two sha256s in it. An id has to
    # be typed, so it carries the name and eight hex of the identifier
    workspace, _ = prepared(tmp_path, [STUCK])
    stalling(workspace, 3)

    item = items(workspace)[STALLED]

    assert item.id.startswith("stalled:stuck:")
    assert len(item.id) < 40
    assert item.subject not in item.id
    assert "stuck" in item.summary


def test_an_action_that_could_not_be_carried_out_is_the_agent_s(
    tmp_path: Path,
) -> None:
    # a real failure rather than a constructed one: an orphan to archive, and
    # a `logs-archive` that is a file, so the move cannot happen
    orphan = SynthTask("removed")
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    write_log(workspace.logs, orphan)
    workspace.logs_archive.write_bytes(b"in the way")

    item = items(workspace)[ACTION_FAILED]

    assert item.owner is Owner.AGENT
    assert item.level is Level.ATTENTION
    # and it is the one kind nothing can dispose of: a single-turn fact, where
    # acknowledging would silence a recurrence rather than an item
    assert not item.acknowledgeable
    assert "_transient_" in workspace.status.read_text(encoding="utf-8")


def test_a_blocked_item_stops_a_run_that_is_otherwise_working(
    tmp_path: Path,
) -> None:
    # no kind produces `BLOCKING` until the parked worker arrives with step 20,
    # so the level is exercised where it is consumed rather than where it will
    # one day be produced
    blocked = Item(
        id="parked:1",
        kind="parked",
        owner=Owner.HUMAN,
        level=Level.BLOCKING,
        subject="1",
        summary="waiting on an approval",
    )

    assert (
        verdict([blocked], paused=False, running=8, spawning=0, unfinished=4)
        is Verdict.STOPPED
    )


# --- the id is the re-notification policy -------------------------------


def test_acknowledging_removes_an_item_from_the_turn_entirely(
    tmp_path: Path,
) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")

    ack(workspace, "unreadable:broken.eval")
    result = turn(workspace)

    assert result.items == []
    assert result.verdict is Verdict.CLEAR
    # and it is gone from what a reader sees, not merely from the list
    assert "broken.eval" not in workspace.status.read_text()


def test_a_second_edit_is_heard_again(tmp_path: Path) -> None:
    # the whole reason ids are keyed the way they are: accepting one deliberate
    # edit must not accept every future one, or the check is off for the run
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.root / DEFINITION).write_bytes(b"# first edit\n")

    ack(workspace, items(workspace)[DRIFT].id)
    assert DRIFT not in items(workspace)

    (workspace.root / DEFINITION).write_bytes(b"# second edit\n")

    assert DRIFT in items(workspace)


def test_acknowledging_one_bad_file_does_not_silence_the_next(
    tmp_path: Path,
) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "first.eval").write_bytes(b"not a log")

    ack(workspace, "unreadable:first.eval")
    (workspace.logs / "second.eval").write_bytes(b"also not a log")

    (item,) = turn(workspace).items
    assert item.id == "unreadable:second.eval"


def test_a_stall_that_gets_worse_is_a_new_item(tmp_path: Path) -> None:
    # keyed on the attempt count, so a task that fails again after somebody
    # accepted the last failure is worth saying again
    workspace, _ = prepared(tmp_path, [STUCK])
    stalling(workspace, 3)
    first = items(workspace)[STALLED]

    ack(workspace, first.id)
    assert STALLED not in items(workspace)

    # somebody intervened, it was tried once more, and it got no further
    stalling(workspace, 1, first=20)

    later = items(workspace).get(STALLED)
    assert later is not None and later.id != first.id


# --- the verdict --------------------------------------------------------

VERDICTS: list[tuple[str, list[Level], bool, int, int, int, Verdict]] = [
    ("nothing open", [], False, 2, 0, 1, Verdict.CLEAR),
    ("paused beats everything", [Level.BLOCKING], True, 0, 0, 3, Verdict.PAUSED),
    ("paused beats a clear run", [], True, 2, 0, 1, Verdict.PAUSED),
    ("work continues around it", [Level.ATTENTION], False, 4, 0, 2, Verdict.ATTENTION),
    ("something blocks", [Level.BLOCKING], False, 4, 0, 2, Verdict.STOPPED),
    ("nothing left to run it", [Level.ATTENTION], False, 0, 0, 1, Verdict.STOPPED),
    ("a slot will free up", [Level.ATTENTION], False, 0, 1, 1, Verdict.ATTENTION),
    ("finished, with a caveat", [Level.ATTENTION], False, 0, 0, 0, Verdict.ATTENTION),
]


@pytest.mark.parametrize(
    ("levels", "paused", "running", "spawning", "unfinished", "expected"),
    [case[1:] for case in VERDICTS],
    ids=[case[0] for case in VERDICTS],
)
def test_the_verdict_describes_the_run_rather_than_its_worst_item(
    levels: list[Level],
    paused: bool,
    running: int,
    spawning: int,
    unfinished: int,
    expected: Verdict,
) -> None:
    given = [
        Item(
            id=f"k:{n}",
            kind="k",
            owner=Owner.HUMAN,
            level=level,
            subject="",
            summary="",
        )
        for n, level in enumerate(levels)
    ]

    assert (
        verdict(
            given,
            paused=paused,
            running=running,
            spawning=spawning,
            unfinished=unfinished,
        )
        is expected
    )


def test_the_verdict_line_says_who_is_holding_it(tmp_path: Path) -> None:
    # it is a notification's title (step 24), so it stands alone: the state and
    # who has to act, and nothing about which item
    mine = Item(
        id="a",
        kind="k",
        owner=Owner.HUMAN,
        level=Level.ATTENTION,
        subject="",
        summary="",
    )
    theirs = Item(
        id="b",
        kind="k",
        owner=Owner.AGENT,
        level=Level.ATTENTION,
        subject="",
        summary="",
    )

    assert verdict_line(Verdict.CLEAR, []) == "✅ nothing needs you"
    assert verdict_line(Verdict.ATTENTION, [mine]) == "⚠️ 1 needs a person"
    assert (
        verdict_line(Verdict.ATTENTION, [mine, theirs])
        == "⚠️ 1 needs a person, 1 for the agent"
    )
    assert verdict_line(Verdict.PAUSED, [mine]).startswith("⏸ paused")
    assert "nothing is progressing" in verdict_line(Verdict.STOPPED, [mine])


# --- the diff -----------------------------------------------------------


def test_a_turn_reports_what_changed_since_the_last_one(tmp_path: Path) -> None:
    # what step 24 fires on, and why it is set arithmetic rather than counts:
    # one item resolved and another arriving in the same turn must not read as
    # no change
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "first.eval").write_bytes(b"not a log")

    first = turn(workspace)
    assert first.appeared == ["unreadable:first.eval"]
    assert first.resolved == []

    (workspace.logs / "first.eval").unlink()
    (workspace.logs / "second.eval").write_bytes(b"not a log")

    second = turn(workspace)
    assert second.appeared == ["unreadable:second.eval"]
    assert second.resolved == ["unreadable:first.eval"]


def test_a_condition_that_persists_is_not_reported_as_new(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")

    turn(workspace)
    second = turn(workspace)

    assert second.appeared == [] and second.resolved == []
    assert [item.id for item in second.items] == ["unreadable:broken.eval"]


def test_a_status_diffs_against_the_last_tend_without_recording_one(
    tmp_path: Path,
) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    turn(workspace)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")

    before = len(workspace.journal.read_text().splitlines())
    previewed = status(workspace)

    assert previewed.appeared == ["unreadable:broken.eval"]
    assert len(workspace.journal.read_text().splitlines()) == before
    # and a status twice running says the same thing, since nothing advanced
    assert status(workspace).appeared == previewed.appeared


# --- supervision ---------------------------------------------------------
#
# The item a timer creates the need for, and the one condition in this file that
# is about Steward's own machinery rather than about the run. Its whole design
# problem is staying quiet: a workspace somebody is driving by hand has not lost
# supervision, so the item is about an *expectation that broke* and there has to
# have been one.


def armed(
    workspace: Workspace,
    *,
    interval: int = 600,
    scheduler: str = "cron",
    ago: int = 0,
) -> None:
    """Record a timer, optionally as of `ago` seconds in the past.

    A silence has to be longer than the timer that failed to fill it, so any case
    about staleness needs an arming that predates the gap — `ago` is how a
    clock-free projection is told that. The payload's `ts` overrides the envelope's
    because `append_event` merges the fields last.
    """
    stamp = datetime.now(timezone.utc) - timedelta(seconds=ago)
    append_event(
        workspace.journal,
        ARMED,
        ts=stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        scheduler=scheduler,
        interval=interval,
        label="steward-aaa",
    )


def aged(workspace: Workspace, seconds: int) -> None:
    """Backdate every recorded turn, as if the timer had stopped firing.

    The only thing a clock-free projection can be given: the item compares the
    last turn's timestamp against now, so making a run look unsupervised means
    moving the timestamps rather than the clock. Goes with `armed(ago=...)` — a
    gap only counts against a timer that was there for it.
    """
    moved = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    lines = workspace.journal.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        event: dict[str, Any] = json.loads(line)
        if event["type"] == OBSERVATION:
            event["ts"] = moved.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
        rewritten.append(json.dumps(event))
    workspace.journal.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def unfinished(tmp_path: Path) -> Workspace:
    """A run with work left, which is the only kind supervision matters for."""
    workspace, _ = prepared(tmp_path, [TASK, SynthTask("waiting")])
    write_log(workspace.logs, TASK)
    return workspace


def test_a_run_nobody_ever_armed_is_not_reported_as_unsupervised(
    tmp_path: Path,
) -> None:
    # a workspace somebody is driving by hand at their terminal, and telling
    # them so every ten minutes is how an attention list stops being read
    workspace = unfinished(tmp_path)

    assert UNSUPERVISED not in items(workspace)


def test_a_timer_that_was_armed_and_is_gone_is_reported(tmp_path: Path) -> None:
    workspace = unfinished(tmp_path)
    armed(workspace)
    append_event(workspace.journal, DISARMED, scheduler="cron")

    item = items(workspace)[UNSUPERVISED]

    assert item.owner is Owner.HUMAN
    assert item.action == "steward timer arm"
    assert item.acknowledgeable
    # acknowledging says *I am driving this by hand*, which stays true -- so no
    # discriminator, and disarming again is the same statement not a new one
    assert item.id == UNSUPERVISED


def test_a_timer_tending_on_time_says_nothing(tmp_path: Path) -> None:
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600)
    turn(workspace)

    assert UNSUPERVISED not in {item.kind for item in status(workspace).items}


def test_a_timer_that_has_stopped_firing_is_reported(tmp_path: Path) -> None:
    # the better signal of the two, because it catches a crontab somebody
    # edited by hand as well as one Steward removed
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)
    turn(workspace)
    aged(workspace, 3600)

    (item,) = [entry for entry in status(workspace).items if entry.kind == UNSUPERVISED]

    assert "has not tended" in item.summary
    assert "10m" in item.summary
    assert item.subject == "cron"


def test_a_recovery_tend_does_not_report_the_silence_it_just_ended(
    tmp_path: Path,
) -> None:
    """The staleness half is vacuous during a tend and meaningful during a `status`.

    The gap is measured against the previous turn, so a tend that has just
    converged the run would otherwise announce the very silence it broke — and
    the reader who needs telling that supervision stopped is the person typing
    `status`, not the timer that is evidently working.
    """
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)
    turn(workspace)
    aged(workspace, 3600)
    assert UNSUPERVISED in {entry.kind for entry in status(workspace).items}

    assert UNSUPERVISED not in {entry.kind for entry in turn(workspace).items}


def test_re_arming_resets_the_clock_the_staleness_check_reads(tmp_path: Path) -> None:
    """A timer armed a moment ago has not been silent for the hours before it existed.

    Otherwise `steward timer arm` — the remedy the item's own `action` names —
    appears not to have worked: the operator arms a replacement and `status`
    immediately tells them the new timer has been quiet since last night.
    """
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)
    turn(workspace)
    aged(workspace, 3600)
    assert UNSUPERVISED in {entry.kind for entry in status(workspace).items}

    armed(workspace, interval=600)

    assert UNSUPERVISED not in {entry.kind for entry in status(workspace).items}


def test_a_timer_armed_and_never_tended_at_all_is_reported(tmp_path: Path) -> None:
    # the other direction the same comparison closes: with nothing but the last
    # tend to go on, a run that has never had one reads as *no evidence of a
    # problem* when it is the plainest case of one
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)

    (item,) = [entry for entry in status(workspace).items if entry.kind == UNSUPERVISED]

    assert "has not tended" in item.summary


def test_a_stale_timer_re_armed_asks_the_question_again(tmp_path: Path) -> None:
    # keyed on when this timer was armed, so an acknowledged silence does not
    # cover the next timer's
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)
    turn(workspace)
    aged(workspace, 3600)
    first = [e for e in status(workspace).items if e.kind == UNSUPERVISED][0]

    ack(workspace, first.id)
    # an hour ago rather than now, because the replacement has to have had a
    # chance to fire and missed it -- re-arming this instant is quiet, which is
    # the case above
    armed(workspace, interval=600, ago=3600)

    second = [e for e in status(workspace).items if e.kind == UNSUPERVISED]
    assert second and second[0].id != first.id


def test_a_finished_run_needs_no_timer(tmp_path: Path) -> None:
    # signing a run off disarms its timer deliberately, and reporting that as
    # lost supervision would make the last act of every run produce an item
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    armed(workspace)
    append_event(workspace.journal, DISARMED, scheduler="cron")

    assert UNSUPERVISED not in items(workspace)


def test_an_interval_the_workspace_no_longer_asks_for_is_reported(
    tmp_path: Path,
) -> None:
    workspace = unfinished(tmp_path)
    armed(workspace, interval=1800)
    workspace.directives.write_text("---\ntend_interval: 5m\n---\n", encoding="utf-8")

    item = items(workspace)[TIMER_DRIFT]

    assert "every 30m" in item.summary and "asks for 5m" in item.summary
    assert item.action == "steward timer arm"


def test_editing_the_interval_again_asks_the_question_again(tmp_path: Path) -> None:
    # both values key the id, so a further edit is a new question and arming to
    # match clears it
    workspace = unfinished(tmp_path)
    armed(workspace, interval=1800)
    workspace.directives.write_text("---\ntend_interval: 5m\n---\n", encoding="utf-8")
    first = items(workspace)[TIMER_DRIFT]

    ack(workspace, first.id)
    workspace.directives.write_text("---\ntend_interval: 1m\n---\n", encoding="utf-8")

    assert items(workspace)[TIMER_DRIFT].id != first.id


def test_a_timer_armed_at_the_interval_asked_for_is_quiet(tmp_path: Path) -> None:
    workspace = unfinished(tmp_path)
    armed(workspace, interval=1800)
    workspace.directives.write_text("---\ntend_interval: 30m\n---\n", encoding="utf-8")

    assert TIMER_DRIFT not in items(workspace)


def test_a_broken_steward_md_does_not_also_report_timer_drift(
    tmp_path: Path,
) -> None:
    # one complaint per broken file: the degraded item already says the file
    # could not be read, and a second saying its interval disagrees would be
    # reporting a value nobody could have written
    workspace = unfinished(tmp_path)
    armed(workspace, interval=1800)
    turn(workspace)
    workspace.directives.write_text("---\nnot: [valid\n---\n", encoding="utf-8")

    found = items(workspace)
    assert DEGRADED in found
    assert TIMER_DRIFT not in found


def test_a_one_off_interval_against_a_silent_file_is_not_drift(
    tmp_path: Path,
) -> None:
    """The comparison is against what the workspace *expressed*, not what resolved.

    An operator who armed `--interval 1m` against a `_steward.md` with no
    opinion about intervals has not created a conflict — and reporting one
    would be reporting drift from Steward's own default, a number nobody wrote.
    Found by arming a real timer and being told, one second later, that the
    workspace wanted something else.
    """
    workspace = unfinished(tmp_path)
    armed(workspace, interval=60)

    assert TIMER_DRIFT not in items(workspace)
