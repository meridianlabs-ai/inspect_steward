"""The agent's queue: what it is shown, what it stops being shown, and why.

Driven through the CLI, because the queue is not a data structure anybody can
hold — it is what `collect` prints, and every property below is a claim about
one command's output after another command ran. Layer 1 throughout: a
synthesized log directory and a real journal, no processes.

Two shapes are under test and they are deliberately not the same one
(agent.md §2.2). **Items are a set with a per-item lifecycle** — one leaves the
queue because somebody *acted*, never because somebody read. **History is a
stream with a cursor** — it leaves because somebody *read*, which is the only
thing that could. An earlier design governed both with the cursor, and the
failure it produced is the case `test_raising...` pins.

The rule that makes the filter safe gets its own case: **no omission is
silent**. A test that only checked absence would pass against the version this
rule exists to rule out, since that version is exactly *absent, with nothing
saying so*.
"""

import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from inspect_steward._cli.items import match_item
from inspect_steward._cli.main import steward
from inspect_steward._evalset.manifest import read_manifest
from inspect_steward._tend.items import Item, Level, Owner
from inspect_steward._worker import LiveFleet, LiveParked, LiveSamples, LiveTask
from inspect_steward._workspace import (
    Workspace,
    create_workspace,
    read_collected,
    read_journal,
)

from .._logs import DEFINITION, SynthTask, write_log
from .test_items import STUCK, stalling
from .test_tend import prepared

DONE = SynthTask("done")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A finished run with one decision of each ownership, and a little history.

    The drift is the human's and the unreadable file is the agent's, which is
    what makes *raised* observable at all: raising is only ever the right verb
    for something the agent cannot itself close.
    """
    create_workspace(tmp_path, git=False)
    workspace, _ = prepared(tmp_path, [DONE])
    write_log(workspace.logs, DONE)
    (workspace.root / DEFINITION).write_bytes(b"# edited\n")
    (workspace.logs / "broken.eval").write_bytes(b"not a log")
    monkeypatch.chdir(workspace.root)
    return workspace


def run(*argv: str) -> str:
    result = CliRunner().invoke(steward, list(argv))
    assert result.exit_code == 0, result.output
    return result.output


def sections(output: str) -> tuple[str, str]:
    """A collection split into what needs deciding and what has happened.

    Sliced rather than searched, because half these assertions are about a
    string being *absent* — and a task name absent from the decisions but
    present in the table below it would pass a whole-document search.
    """
    body, _, history = output.partition("## what happened")
    _, _, decisions = body.partition("## what needs a decision")
    return decisions.partition("## the run")[0], history


def history(workspace: Workspace) -> None:
    """Two admitted events, so there is a stream for the cursor to be about."""
    run("pause", "--reason", "thinking about it")
    run("resume")


# --- the cursor governs history, and nothing else -----------------------


def test_a_second_collection_shows_what_the_first_did_not(
    workspace: Workspace,
) -> None:
    history(workspace)

    first = run("collect")
    run("ack", "unreadable", "--reason", "a stray file", "--by", "agent")
    second = run("collect")

    assert "paused by human — thinking about it" in first
    # the ack is the only thing that happened since, and the two entries the
    # first collection already showed are counted rather than repeated
    _, latest = sections(second)
    assert "accepted by agent" in latest
    assert "paused by human" not in latest
    assert "2 earlier, not shown — `--since 0` for all." in latest


def test_a_collection_that_landed_leaves_nothing_new(workspace: Workspace) -> None:
    history(workspace)

    run("collect")
    _, latest = sections(run("collect"))

    # *nothing new* rather than *nothing*, with the count: an agent can read a
    # label but cannot reason about what it was never shown
    assert "Nothing new. 2 earlier — `--since 0` for all." in latest


def test_a_collection_that_did_not_land_is_offered_again(
    workspace: Workspace,
) -> None:
    # the crash case, and the reason `--peek` exists: an agent that read and
    # died must find its work waiting rather than having consumed it by looking
    history(workspace)

    run("collect", "--peek")
    _, latest = sections(run("collect"))

    assert "paused by human — thinking about it" in latest
    assert read_collected(read_journal(workspace.journal).events) is not None


def test_reaching_back_does_not_uncollect_what_came_after(
    workspace: Workspace,
) -> None:
    history(workspace)
    run("collect")

    reached = run("collect", "--since", "0")
    _, latest = sections(run("collect"))

    # `--since` governs what is *shown*; the collection still records how far
    # the agent has now read, so re-reading the night does not replay it
    assert "paused by human — thinking about it" in reached
    assert "Nothing new." in latest


# --- items leave because somebody acted ---------------------------------


def test_raising_takes_an_item_out_of_the_queue_and_leaves_it_open(
    workspace: Workspace,
) -> None:
    """The state an item-as-stream design has no room for.

    Only a human can close a human-owned item, so without this the agent's own
    queue holds it at every collection all night — sixty appearances of one
    decision the agent has already done everything about.
    """
    run("raise", "drift", "--note", "asked in #evals")

    collected, _ = sections(run("collect"))
    summary = run("status", "--format", "md")

    assert "the definition has changed" not in collected
    # still open, still the person's, and still counted by the verdict: what
    # raising records is that the *agent's* part is done
    assert "the definition has changed" in summary
    assert "⚠️ 2 need a person" in summary


def test_nothing_the_projection_sets_aside_is_dropped_silently(
    workspace: Workspace,
) -> None:
    """The rule that makes the filter safe, asserted as its own case.

    A shortened list with nothing saying so invites an agent to conclude there
    are no open decisions when one is sitting with a human — and a test that
    only checked absence would pass against exactly that version.
    """
    run("raise", "drift", "--note", "asked in #evals")
    run("raise", "signoff", "--note", "sent the results over")

    collected, _ = sections(run("collect"))

    assert "2 raised, awaiting a person" in collected


def test_a_queue_with_nothing_left_in_it_still_says_what_is_pending(
    workspace: Workspace,
) -> None:
    run("ack", "unreadable", "--reason", "a stray file", "--by", "agent")
    run("raise", "drift", "--note", "asked in #evals")
    run("raise", "signoff", "--note", "sent the results over")

    collected, _ = sections(run("collect"))

    assert "Nothing for you. 2 raised, awaiting a person." in collected


def test_a_condition_that_changes_comes_back_as_new_work(tmp_path: Path) -> None:
    """Re-entry needs no expiry rule, because an id encodes the instance.

    A task that stalls again at a later attempt is a different id from the one
    that stalled before it, so it arrives as work; an unchanged condition stays
    raised and stays quiet.
    """
    create_workspace(tmp_path, git=False)
    workspace, _ = prepared(tmp_path, [STUCK])
    stalling(workspace, 3)
    os.chdir(workspace.root)

    first = run("status", "--format", "md")
    raised = next(
        line.split("`")[-2] for line in first.splitlines() if "`stalled:" in line
    )
    run("raise", raised, "--note", "asked whether to keep retrying")
    quiet, _ = sections(run("collect"))

    stalling(workspace, 1, first=20)
    returned, _ = sections(run("collect"))

    assert "stuck" not in quiet
    assert "stuck" in returned


def test_a_note_appears_under_what_happened(workspace: Workspace) -> None:
    # a note is written for the next reader, and collect is the next reader
    run("note", "sonnet arm failing since 01:40; suspect the provider")

    _, latest = sections(run("collect"))

    assert "noted by agent" in latest and "suspect the provider" in latest


def test_acknowledging_moves_an_item_from_the_queue_into_what_happened(
    workspace: Workspace,
) -> None:
    # a disposal that erased itself would take *somebody dealt with this at
    # 2am, and here is why* with it, which is what a 6am reader has no other
    # way to learn
    run("ack", "unreadable", "--reason", "a stray file", "--by", "agent")

    collected, latest = sections(run("collect"))

    assert "broken.eval" not in collected
    assert "accepted by agent" in latest and "a stray file" in latest


# --- naming an item -----------------------------------------------------


def test_a_prefix_that_names_more_than_one_item_is_refused(
    workspace: Workspace,
) -> None:
    # guessing between two items somebody is about to record a decision
    # against is the one outcome worth refusing outright
    (workspace.logs / "broken-too.eval").write_bytes(b"not a log either")

    result = CliRunner().invoke(
        steward, ["ack", "unreadable:broken", "--reason", "stray files"]
    )

    assert result.exit_code != 0
    assert "matches 2 items" in result.output


def test_an_exact_id_wins_before_prefixes_are_considered() -> None:
    """Which `stalled:t:2` and `stalled:t:20` make a real case, not a theoretical one.

    Unreachable from a workspace without contriving two stalls whose attempt
    counts nest, and the rule is the whole reason the matcher is shared — so it
    is asserted where it lives.
    """

    def item(identifier: str) -> Item:
        return Item(
            id=identifier,
            kind="stalled",
            owner=Owner.HUMAN,
            level=Level.ATTENTION,
            subject="t",
            summary=identifier,
        )

    items = [item("stalled:t:2"), item("stalled:t:20")]

    assert match_item(items, "stalled:t:2") is items[0]
    assert match_item(items, "stalled:t:20") is items[1]
    assert match_item(items, "missing") is None


def test_raising_again_records_the_newer_note_and_says_it_is_not_the_first(
    workspace: Workspace,
) -> None:
    # an open item is still nameable after it has been raised, and chasing a
    # decision a second time is real work worth recording -- so this appends
    # rather than refusing, and says which it did
    run("raise", "drift", "--note", "asked in #evals")

    again = run("raise", "drift", "--note", "chased it in standup")

    assert "already raised" in again
    _, latest = sections(run("collect"))
    assert "chased it in standup" in latest


def test_raising_something_that_is_no_longer_open_says_when_it_was_raised(
    workspace: Workspace,
) -> None:
    # raising closes nothing, so reaching this message means the item went away
    # for some other reason — here, the person it was handed to accepted it
    run("raise", "drift", "--note", "sent the diff over")
    run("ack", "drift", "--reason", "the edit was mine")

    result = CliRunner().invoke(steward, ["raise", "drift"])

    assert result.exit_code != 0
    assert "no longer open" in result.output
    assert "sent the diff over" in result.output


def test_an_agent_owned_item_cannot_be_raised(workspace: Workspace) -> None:
    """Because raising it would strand it, which is worse than not working.

    An item leaves the agent's queue when it is raised and comes back only if
    the condition changes. Applied to something the agent itself owns, that is
    a dead end: open forever, gone from the one list that would have returned
    it, and owned by nobody who is looking.
    """
    result = CliRunner().invoke(steward, ["raise", "unreadable"])

    assert result.exit_code != 0
    assert "the agent's own to resolve" in result.output
    assert "steward ack --by agent" in result.output
    # and it is genuinely still there, in both projections
    collected, _ = sections(run("collect"))
    assert "broken.eval" in collected
    assert "broken.eval" in run("status", "--format", "md")


# --- a park: raisable, never acknowledgeable ----------------------------


@pytest.fixture
def parked(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """The same workspace, with its one worker waiting on an approval.

    A park lives on a running worker's socket, so the fleet read is what gets
    substituted — see the note in `test_items.py`. Everything downstream of it,
    including both commands under test, is the real thing.
    """
    manifest = read_manifest(workspace.manifest)
    fleet = LiveFleet(
        tasks={
            manifest.tasks[0].identifier: LiveTask(
                pid=os.getpid(),
                identifier=manifest.tasks[0].identifier,
                samples=LiveSamples(total=4, completed=0, in_flight=1),
                parked=LiveParked(approvals=1, functions=("bash",)),
            )
        }
    )

    def read(inflight: object, logs: object, *, stuck_after: float = 0.0) -> LiveFleet:
        return fleet

    monkeypatch.setattr("inspect_steward._tend.turn._live", read)
    return workspace


def test_a_park_can_be_raised_even_though_it_cannot_be_acknowledged(
    parked: Workspace,
) -> None:
    """The gate is ownership alone, and this pair is why.

    Under the old gate — acknowledgeable *and* human-owned — a park could be
    neither acked nor raised, so it sat in the agent's queue at every
    collection all night, which is the exact failure `raise` exists to prevent.
    """
    collected, _ = sections(run("collect"))
    assert "waiting on an approval for bash" in collected

    assert "raised parked:" in run("raise", "parked", "--note", "texted them")

    # out of the agent's queue and still in the person's, because a person
    # still owes an answer
    after, _ = sections(run("collect"))
    assert "waiting on an approval" not in after
    assert "1 raised" in after
    assert "waiting on an approval" in run("status", "--format", "md")


def test_a_park_cannot_be_acknowledged_and_the_refusal_says_why(
    parked: Workspace,
) -> None:
    # the other unacknowledgeable kind is a single-turn fact; a park is the most
    # standing condition there is, and telling somebody the wrong one is worse
    # than saying nothing
    result = CliRunner().invoke(
        steward, ["ack", "parked", "--reason", "somebody is on it"]
    )

    assert result.exit_code != 0
    assert "only answering it clears it" in result.output
    assert "steward raise" in result.output
