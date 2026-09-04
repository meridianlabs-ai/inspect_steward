"""What a turn is worth telling somebody, driven through real turns.

Every trigger is a set diff between turns, so nothing here constructs a
`TendResult` by hand — the condition is created in the workspace and the
assertion is about what came out. Two properties carry the design. **One post
per turn, whatever changed**, because a turn is one moment and a reader wants
one message about it. And **an edge rather than a level**: the same condition
persisting must not post again, which is the difference between a channel
somebody reads and one they mute.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from inspect_steward._evalset.manifest import write_manifest
from inspect_steward._notify import (
    INSPECT_NOTIFICATION,
    Delivery,
    Kind,
    Post,
)
from inspect_steward._tend import Verdict, turn_post
from inspect_steward._tend.notify import LINES, ROWS, SAID, UNATTENDED_INTERVALS
from inspect_steward._worker import LiveParked
from inspect_steward._workspace import (
    ACKNOWLEDGED,
    COLLECTED,
    DEFAULT_TEND_INTERVAL,
    PAUSED,
    Workspace,
    append_event,
    read_journal,
    read_undelivered,
)

from .._acp import Publish, publish
from .._logs import DEFINITION, SynthTask, synth_manifest, write_log, write_unreadable
from ..schedule.test_items import parked_run
from ..schedule.test_tend import prepared, turn

DONE = SynthTask("done")
OTHER = SynthTask("other")
THIRD = SynthTask("third")
CHANNEL = "slack://xoxb-1234567890-1234567890-abcdefghij/#general"

__all__ = ["publish"]
PENDING = SynthTask("pending")


def paused(workspace: Workspace) -> None:
    """Hold the fleet, so a turn over an unfinished run spawns nothing.

    What is being isolated is the `progress` trigger, and a run with work left
    is the only shape in which it fires on its own — the gate outranks it the
    moment the last task lands. A pause is the cheapest way to have work left
    and no worker started for it.
    """
    append_event(workspace.journal, PAUSED, by="operator", reason="under test")


HORIZON = UNATTENDED_INTERVALS * DEFAULT_TEND_INTERVAL
"""How long a collection may be stale before the agent's items become an operator's."""


def collected(workspace: Workspace, *, ago: float = 0.0) -> None:
    """Record an agent collection, optionally back-dated.

    Written as a line rather than through `append_event`, which stamps the
    present: the whole question here is how long ago it was, and the
    alternative is a test that sleeps for twenty minutes.
    """
    when = datetime.now(timezone.utc) - timedelta(seconds=ago)
    event = {
        "ts": when.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "type": COLLECTED,
        "position": 0,
    }
    with workspace.journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


def test_a_first_turn_says_nothing_about_what_was_already_finished(
    tmp_path: Path,
) -> None:
    # absent and empty are different: a run tended by a Steward that predates
    # the recorded set would otherwise post one message naming every task it
    # had ever finished, on the first turn after an upgrade
    workspace, _ = prepared(tmp_path, [DONE, OTHER])
    write_log(workspace.logs, DONE)
    write_log(workspace.logs, OTHER)

    result = turn(workspace)

    assert result.finished == []
    # the gate still posts, and should: the run *is* waiting to be accepted,
    # and that is an item appearing rather than a completion being diffed
    post = turn_post(result)
    assert post is not None and post.kind is Kind.GATE
    assert not [line for line in post.lines if line.startswith("finished ")]


def test_tasks_finishing_produce_one_post_naming_all_of_them(tmp_path: Path) -> None:
    # the tend is already the clock, so batching is free -- and a post per task
    # is the noise fatigue the whole design opens with
    workspace, _ = prepared(tmp_path, [DONE, OTHER, THIRD, PENDING])
    paused(workspace)
    turn(workspace)
    for task in (DONE, OTHER, THIRD):
        write_log(workspace.logs, task)

    result = turn(workspace)
    post = turn_post(result)

    assert len(result.finished) == 3
    assert post is not None
    assert post.kind is Kind.PROGRESS
    assert sum(1 for line in post.lines if line.startswith("finished ")) == 3


def test_a_task_that_stays_finished_is_not_news_again(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)
    turn(workspace)

    result = turn(workspace)

    assert result.finished == []


def test_the_gate_posts_once_when_the_run_settles(tmp_path: Path) -> None:
    # `signoff_ready` persists until somebody answers, so what stops it posting
    # every ten minutes all night is the item diff -- and its id is keyed on the
    # manifest digest, which is what re-arms it when a launch changes the tasks
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)

    settled = turn(workspace)
    again = turn(workspace)

    assert settled.verdict is Verdict.COMPLETE
    post = turn_post(settled)
    assert post is not None and post.kind is Kind.GATE
    assert turn_post(again) is None


def test_the_post_leads_with_the_verdict_line(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)

    post = turn_post(turn(workspace))

    assert post is not None
    assert post.glyph == Verdict.COMPLETE.value
    assert post.title == "complete (the results are waiting to be accepted)"


def test_the_table_rides_at_two_widths(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)

    post = turn_post(turn(workspace))

    assert post is not None
    assert post.table and post.narrow
    assert post.monospace(narrow=True) == post.narrow
    assert post.monospace(narrow=False) == post.table


def test_a_long_list_says_what_it_left_out(tmp_path: Path) -> None:
    # the discipline `status.md` already keeps: a shortened list with nothing
    # saying so reads as the whole of it
    many = [SynthTask(f"task{index}") for index in range(LINES + ROWS + 2)]
    workspace, _ = prepared(tmp_path, [*many, PENDING])
    paused(workspace)
    turn(workspace)
    for task in many:
        write_log(workspace.logs, task)

    post = turn_post(turn(workspace))

    assert post is not None
    assert sum(1 for line in post.lines if line.startswith("finished ")) == LINES
    assert any(
        line.startswith(f"and {len(many) - LINES} more tasks") for line in post.lines
    )
    # ROWS rows, the count of what was dropped, and the shared model the table
    # ends with -- the model describes every task, row or no row
    tasks = len(many) + 1
    assert len(post.table) == ROWS + 2
    assert post.table[ROWS] == f"... {tasks - ROWS} more tasks"


def test_a_quiet_turn_posts_nothing(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)
    turn(workspace)

    assert turn_post(turn(workspace)) is None


def test_an_item_appearing_posts_and_the_same_item_persisting_does_not(
    tmp_path: Path,
) -> None:
    # an edge rather than a level. The same condition reported every ten
    # minutes is how an attention channel stops being read
    workspace, _ = prepared(tmp_path, [PENDING])
    paused(workspace)
    turn(workspace)
    (workspace.root / DEFINITION).write_bytes(b"# edited after capture\n")

    appeared = turn(workspace)
    again = turn(workspace)

    post = turn_post(appeared)
    assert post is not None and post.kind is Kind.ATTENTION
    assert len(appeared.appeared) == 1
    assert any("definition" in line for line in post.lines)
    assert turn_post(again) is None


def test_the_queue_emptying_posts_once(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [PENDING])
    paused(workspace)
    turn(workspace)
    (workspace.root / DEFINITION).write_bytes(b"# edited after capture\n")
    raised = turn(workspace)

    (item,) = raised.items
    append_event(
        workspace.journal,
        ACKNOWLEDGED,
        id=item.id,
        kind=item.kind,
        subject=item.subject,
        summary=item.summary,
        by="operator",
        reason="deliberate",
    )
    cleared = turn(workspace)
    again = turn(workspace)

    post = turn_post(cleared)
    assert post is not None and post.kind is Kind.CLEAR
    # and says nothing about what closed: the title is the whole message, and
    # "1 item closed" is a number with no content -- it names nothing, and the
    # reader already knows what they answered
    assert post.lines == []
    assert turn_post(again) is None


def sends(monkeypatch: pytest.MonkeyPatch) -> list[Post]:
    """Intercept the send, so a turn's posting is observable without a notifier."""
    sent: list[Post] = []

    def capture(instance: Any, post: Post, log: Path) -> Delivery:
        sent.append(post)
        return Delivery(landed=1)

    monkeypatch.setattr("inspect_steward._tend.notify.send_post", capture)
    return sent


def test_a_turn_with_a_channel_posts_through_the_real_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    sent = sends(monkeypatch)
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)

    turn(workspace)

    assert [post.kind for post in sent] == [Kind.GATE]


def test_declining_silences_steward_even_with_inspects_variable_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`notification: false` leaves the fleet's channel alone, deliberately.

    A worker's notifications are blocking human-in-the-loop prompts, so
    silencing those would hang a sample with nobody told. The consequence is
    that the variable is still set when Steward's own turn goes to post — and a
    turn that asked the environment again rather than carrying what
    `establish_channel` settled would post anyway, which is the decline not
    taking effect at all.
    """
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    sent = sends(monkeypatch)
    workspace, _ = prepared(tmp_path, [DONE])
    workspace.directives.write_text("notification: false\n", encoding="utf-8")
    turn(workspace)
    write_log(workspace.logs, DONE)

    turn(workspace)

    assert sent == []
    # and the fleet's channel is untouched, which is the other half of the rule
    assert os.environ[INSPECT_NOTIFICATION] == CHANNEL


# --- whose item it is ------------------------------------------------------


def unreadable(workspace: Workspace) -> None:
    """Create the agent's own item: a file listed as a log that will not read."""
    write_unreadable(workspace.logs)


def test_the_agents_own_items_do_not_wake_a_person(tmp_path: Path) -> None:
    # `unreadable` is routed to the agent precisely because an operator is not the
    # one who should look at it, and a post naming it is the channel
    # advertising work its reader was told not to do
    workspace, _ = prepared(tmp_path, [DONE, PENDING])
    paused(workspace)
    collected(workspace)
    turn(workspace)
    unreadable(workspace)

    assert turn_post(turn(workspace)) is None


def test_an_agents_item_reaches_a_person_where_no_agent_ever_attached(
    tmp_path: Path,
) -> None:
    # the thing an operator has to do about a workspace nobody has attached to is
    # attach to it, and never-collected needs no horizon to be sure of
    workspace, _ = prepared(tmp_path, [DONE, PENDING])
    paused(workspace)
    turn(workspace)
    unreadable(workspace)

    post = turn_post(turn(workspace))

    assert post is not None and post.kind is Kind.ATTENTION
    assert any("could not be read as a log" in line for line in post.lines)
    # and the post says the item, not why the item is here: the escalation
    # decides whether to show it, and once shown the routing is not actionable
    assert not any("agent" in line for line in post.lines)


STALENESS = [
    ("collected this turn", 0.0, False),
    ("one tend ago", DEFAULT_TEND_INTERVAL, False),
    ("two tends ago", HORIZON + 1, True),
    ("all night", 8 * HORIZON, True),
]


@pytest.mark.parametrize(
    ("ago", "reaches"),
    [(ago, reaches) for _, ago, reaches in STALENESS],
    ids=[case for case, _, _ in STALENESS],
)
def test_an_agent_that_stopped_collecting_hands_its_items_back(
    tmp_path: Path, ago: float, reaches: bool
) -> None:
    # an agent's item is only the agent's while there is an agent, and the
    # horizon is counted in tends because what is being asked is whether one
    # has had the chance
    workspace, _ = prepared(tmp_path, [DONE, PENDING])
    paused(workspace)
    collected(workspace, ago=ago)
    turn(workspace)
    unreadable(workspace)

    assert (turn_post(turn(workspace)) is not None) is reaches


def test_the_title_counts_what_the_reader_has_to_act_on(tmp_path: Path) -> None:
    # `verdict_line` splits *needs an operator* from *for the agent* because its
    # readers include the agent. Here there is one reader and everything in
    # front of them is theirs, so the split would be Steward's bookkeeping
    #
    # A settled run rather than a paused one: the pause has a verdict line of
    # its own, and what is being read here is the counting clause
    workspace, _ = prepared(tmp_path, [DONE])
    write_log(workspace.logs, DONE)
    turn(workspace)
    unreadable(workspace)

    post = turn_post(turn(workspace))

    assert post is not None
    assert post.glyph == Verdict.ATTENTION.value
    assert post.title == "1 decision needs attention"
    assert "for the agent" not in post.title


def test_a_persons_item_is_never_marked_by_owner(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)

    post = turn_post(turn(workspace))

    assert post is not None
    assert not any("for the agent" in line for line in post.lines)


def test_the_gate_does_not_restate_its_own_title(tmp_path: Path) -> None:
    # `verdict()` returns COMPLETE exactly when every open item is
    # `signoff_ready`, so the line `verdict_line` writes for that verdict is
    # that item's own sentence -- and a bullet repeating it adds a task count
    # the table already carries
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)

    post = turn_post(turn(workspace))

    assert post is not None and post.kind is Kind.GATE
    assert not any("waiting to be accepted" in line for line in post.lines)
    # what changed to produce it is still said
    assert post.lines == ["finished done"]


def test_an_item_carrying_somebody_elses_exception_is_trimmed(
    tmp_path: Path,
) -> None:
    # `unreadable`, `degraded` and `action_failed` embed an exception, which
    # arrives with an absolute path and no length in principle. `status.md` has
    # the whole of it, on a screen; a phone gets the sentence
    workspace, _ = prepared(tmp_path, [DONE, PENDING])
    paused(workspace)
    turn(workspace)
    unreadable(workspace)

    result = turn(workspace)
    item = next(one for one in result.items if one.kind == "unreadable")
    post = turn_post(result)

    assert len(item.summary) > SAID, "the premise: this one runs long"
    assert post is not None
    trimmed = post.lines[0]
    assert trimmed.endswith("…") and len(trimmed) <= SAID + 1


def test_a_summary_short_enough_to_read_is_left_alone(tmp_path: Path) -> None:
    # and Steward's own CLI does not travel with it: `steward launch` is right
    # on the item, where `status.md`'s reader is the agent, and printing it into
    # a channel invites the operator to drive Steward by hand
    workspace, _ = prepared(tmp_path, [PENDING])
    paused(workspace)
    turn(workspace)
    (workspace.root / DEFINITION).write_bytes(b"# edited after capture\n")

    result = turn(workspace)
    post = turn_post(result)

    assert next(one for one in result.items if one.kind == "drift").action == (
        "steward launch"
    )
    assert post is not None
    assert post.lines[0] == "the definition has changed since it was captured"


def test_the_one_command_a_person_runs_does_travel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publish: Publish
) -> None:
    # a park is in `FIXED_OWNER` because the agent may never answer one, which
    # is the same reason its command is the operator's to run -- and it reaches
    # the worker holding a sample hostage, which nothing else ends
    publish(os.getpid(), tmp_path / "w.sock")
    workspace = parked_run(tmp_path, monkeypatch, LiveParked(approvals=1))

    post = turn_post(turn(workspace))

    assert post is not None
    assert post.lines[0].endswith(" (inspect acp)")


def test_a_post_says_which_workspace_it_is_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # one channel serves however many runs an operator has going, and every title
    # here is a sentence about *a* run -- two workspaces posting "2 decisions
    # need attention" an hour apart are otherwise indistinguishable
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    sent = sends(monkeypatch)
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)

    turn(workspace)

    # the glyph sorts ahead of the name: a reader with six runs finds the run
    # by its name, a reader with one finds the urgent post by its character
    assert sent[0].heading == f"{sent[0].glyph} {workspace.root.name}: {sent[0].title}"
    # and neither is baked into the title, which stays about the run
    assert workspace.root.name not in sent[0].title
    assert sent[0].glyph is not None and sent[0].glyph not in sent[0].title


def test_nothing_under_the_table_totals_the_table(tmp_path: Path) -> None:
    # samples, running and queued are a column each in the rows directly above,
    # so a line summing them restates the screen the reader has just read with
    # numbers that are different every ten minutes
    workspace, _ = prepared(tmp_path, [DONE, OTHER, PENDING])
    paused(workspace)
    turn(workspace)
    write_log(workspace.logs, DONE)
    write_log(workspace.logs, OTHER)

    post = turn_post(turn(workspace))

    assert post is not None
    under = post.table[-1]
    assert "samples" not in under and "%" not in under
    assert "running" not in under and "queued" not in under
    # what is left is the model the keys elided, which no row can say
    assert under.strip() == "mockllm/model"


def test_a_task_is_named_as_shortly_as_the_table_names_it(tmp_path: Path) -> None:
    # the part being elided is on screen either way: the table is directly
    # beneath, and a model every row shares is said outright beneath it. So
    # `@mockllm/model` after every task name costs a phone reader a line each
    # time to repeat what the line below already said
    workspace, _ = prepared(tmp_path, [DONE, OTHER, PENDING])
    paused(workspace)
    turn(workspace)
    write_log(workspace.logs, DONE)
    write_log(workspace.logs, OTHER)

    result = turn(workspace)
    post = turn_post(result)

    # the run carries identifiers, which is what it diffs against a record an
    # earlier turn wrote; the post is where they turn back into names
    assert result.finished == sorted(
        row.identifier
        for row in result.progress.rows
        if row.key.startswith(("done", "other"))
    )
    assert post is not None
    assert sorted(post.lines) == ["finished done", "finished other"]
    assert "mockllm/model" in post.table[-1]


def test_a_relaunch_that_renames_a_task_does_not_finish_it_twice(
    tmp_path: Path,
) -> None:
    """A display key is computed against whatever else is on screen.

    `done[default]@mockllm/model` is that key only while nothing else claims
    it; a relaunch adding a task that collides on name, solver and model gives
    both of them an argument suffix — which renames a task that finished
    yesterday, and a diff keyed on the name reads the new spelling as news.
    """
    workspace, manifest = prepared(tmp_path, [DONE, PENDING])
    paused(workspace)
    turn(workspace)
    write_log(workspace.logs, DONE)
    (settled,) = turn(workspace).finished

    colliding = SynthTask("done", args={"difficulty": "hard"})
    relaunched = synth_manifest([DONE, PENDING, colliding])
    write_manifest(
        relaunched.model_copy(update={"source": manifest.source}), workspace.manifest
    )
    result = turn(workspace)

    (renamed,) = [row for row in result.progress.rows if row.identifier == settled]
    assert renamed.key != "done[default]@mockllm/model", "the collision did not bite"
    assert result.finished == []
    post = turn_post(result)
    assert post is None or not [
        line for line in post.lines if line.startswith("finished ")
    ]


def refusing(monkeypatch: pytest.MonkeyPatch, until: int) -> list[Post]:
    """Intercept the send, refusing the first `until` of them."""
    sent: list[Post] = []

    def capture(instance: Any, post: Post, log: Path) -> Delivery:
        sent.append(post)
        if len(sent) <= until:
            return Delivery(failures=["the notifier would not accept it"])
        return Delivery(landed=1)

    monkeypatch.setattr("inspect_steward._tend.notify.send_post", capture)
    return sent


def test_a_post_that_did_not_land_is_owed_to_the_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an edge is consumed by the observation that records it, which is what
    # stops a condition repeating -- and which would otherwise make one
    # unreachable minute at 2am cost the gate permanently
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    sent = refusing(monkeypatch, until=1)
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)

    turn(workspace)
    again = turn(workspace)
    settled = turn(workspace)

    assert [post.kind for post in sent] == [Kind.GATE, Kind.GATE]
    # the run itself did not change; the edge was recomputed from the retained
    # one, and is spent once a post lands
    assert again.verdict is Verdict.COMPLETE
    assert turn_post(settled) is None


def test_a_post_one_target_refused_is_not_owed_to_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a channel is a list, and a mailing list that has been broken since Tuesday
    # must not repost the same gate to Slack every ten minutes until somebody
    # fixes it -- the reader was told, which is the whole question
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    sent: list[Post] = []

    def partly(instance: Any, post: Post, log: Path) -> Delivery:
        sent.append(post)
        return Delivery(landed=1, failures=["could not post to the html channel: no"])

    monkeypatch.setattr("inspect_steward._tend.notify.send_post", partly)
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)

    turn(workspace)
    turn(workspace)

    assert [post.kind for post in sent] == [Kind.GATE]
    assert not read_undelivered(read_journal(workspace.journal).events)[0]


def test_a_channel_that_will_not_build_owes_the_edge_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the reader was not told either way, which is the only thing that matters
    monkeypatch.setenv(INSPECT_NOTIFICATION, CHANNEL)
    monkeypatch.setattr("inspect_steward._tend.notify.channel_apprise", lambda: None)
    workspace, _ = prepared(tmp_path, [DONE])
    turn(workspace)
    write_log(workspace.logs, DONE)
    turn(workspace)

    assert read_undelivered(read_journal(workspace.journal).events)[0]
    post = turn_post(turn(workspace))
    assert post is not None and post.kind is Kind.GATE


def test_declining_still_gives_the_fleet_a_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-notification` says *do not post to me*, not *let a sample hang*.

    A worker's notifications are blocking human-in-the-loop prompts, so a
    decline that took the channel away with it would leave a sample stopped on
    an approval holding its slot, its sandbox and its model connections until
    morning with nobody told.
    """
    monkeypatch.delenv(INSPECT_NOTIFICATION, raising=False)
    sent = sends(monkeypatch)
    workspace, _ = prepared(tmp_path, [DONE])
    workspace.directives.write_text(f"notification: {CHANNEL}\n", encoding="utf-8")
    turn(workspace)
    write_log(workspace.logs, DONE)
    # cleared, so what is asserted below is this turn's own export rather than
    # the one the turn above happened to leave behind
    monkeypatch.delenv(INSPECT_NOTIFICATION, raising=False)

    turn(workspace, notification=False)

    assert sent == []
    assert os.environ[INSPECT_NOTIFICATION] == CHANNEL
