"""The journal, and mostly what it does when something has gone wrong.

The ordinary path is one line of JSON. What earns the tests is the rest: a
crash mid-append, a line that was never valid, an event type written by a
version that does not exist yet, and two processes appending at once. Each of
those is a claim the design makes, and none is visible on a run that goes well.
"""

import json
import multiprocessing
from collections.abc import Callable
from pathlib import Path

import pytest
from inspect_steward._workspace import (
    ACKNOWLEDGED,
    ARMED,
    COLLECTED,
    DISARMED,
    OBSERVATION,
    PAUSED,
    RAISED,
    RESUMED,
    JournalEvent,
    append_event,
    create_workspace,
    read_acks,
    read_armed,
    read_collected,
    read_journal,
    read_pause,
    read_raised,
    summarize,
)


def write_lines(journal: Path, *lines: str) -> None:
    """Write a journal verbatim, so a test can express damage a writer cannot produce."""
    journal.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def event_line(type: str, **fields: object) -> str:
    return json.dumps({"ts": "2026-08-23T19:00:00.000Z", "type": type, **fields})


def test_round_trip(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    append_event(journal, "initialized", definition="evalset.py")
    append_event(journal, "observation", pending=3, running=2)

    read = read_journal(journal)

    assert read.intact
    assert [event.type for event in read.events] == ["initialized", "observation"]
    assert read.events[0].payload == {"definition": "evalset.py"}
    assert read.events[1].payload == {"pending": 3, "running": 2}
    # UTC with an explicit offset, always
    assert all(event.ts.endswith("Z") for event in read.events)


def test_torn_last_line_costs_one_event(tmp_path: Path) -> None:
    # what a crash between the write starting and the newline landing leaves
    journal = tmp_path / "journal.jsonl"
    append_event(journal, "initialized")
    append_event(journal, "observation")
    with journal.open("a", encoding="utf-8") as f:
        f.write('{"ts":"2026-08-23T19:00:00.000Z","type":"observ')

    read = read_journal(journal)

    assert [event.type for event in read.events] == ["initialized", "observation"]
    assert len(read.damage) == 1
    assert read.damage[0].line == 3
    assert "not valid JSON" in read.damage[0].reason


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("{not json at all", "not valid JSON"),
        ('["an", "array"]', "not a JSON object"),
        ('{"type": "observation"}', "missing a string"),
        ('{"ts": "2026-08-23T19:00:00.000Z"}', "missing a string"),
        ('{"ts": 12345, "type": "observation"}', "missing a string"),
    ],
)
def test_a_bad_line_is_reported_and_skipped(
    bad: str, reason: str, tmp_path: Path
) -> None:
    # damage in the middle, not at the tail: the events after it still read
    journal = tmp_path / "journal.jsonl"
    write_lines(journal, event_line("initialized"), bad, event_line("signoff"))

    read = read_journal(journal)

    assert [event.type for event in read.events] == ["initialized", "signoff"]
    assert len(read.damage) == 1
    assert read.damage[0].line == 2
    assert reason in read.damage[0].reason
    assert read.damage[0].text == bad


def test_an_unknown_event_type_still_reads(tmp_path: Path) -> None:
    # a workspace outlives the Steward that wrote it, so an event from a later
    # version is history to be read rather than input to be refused
    journal = tmp_path / "journal.jsonl"
    write_lines(
        journal,
        event_line("initialized"),
        event_line("something_invented_later", shape={"nested": [1, 2]}),
    )

    read = read_journal(journal)

    assert read.intact
    assert read.events[1].type == "something_invented_later"
    assert read.events[1].payload == {"shape": {"nested": [1, 2]}}


def test_a_missing_journal_is_an_empty_history(tmp_path: Path) -> None:
    read = read_journal(tmp_path / "nothing-here.jsonl")

    assert read.events == []
    assert read.intact


def test_blank_lines_are_not_damage(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    write_lines(journal, event_line("initialized"), "", event_line("signoff"))

    read = read_journal(journal)

    assert len(read.events) == 2
    assert read.intact


def append_many(journal: str, type: str, count: int) -> None:
    """Append `count` events, for the concurrency test to run in its own process."""
    for index in range(count):
        append_event(Path(journal), type, index=index)


def test_concurrent_appends_do_not_interleave(tmp_path: Path) -> None:
    """Two processes appending at once produce whole lines, not shredded ones.

    A tend holds the run claim and an agent does not, so concurrent writers are
    the normal case rather than a race to be excluded.

    What this catches, measured rather than assumed: an implementation that
    splits one record across more than one `write` — payload then newline, say —
    loses roughly a quarter of its events to interleaving under four writers.
    What it does not catch is platform-level non-atomicity of a single
    append-mode write, because on a local filesystem there is none; a buffered
    `open(..., "a")` that writes the whole line at once passes this too. The
    guard is against the refactor, not against the operating system.
    """
    journal = tmp_path / "journal.jsonl"
    writers, each = 4, 25

    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=append_many, args=(str(journal), f"writer{n}", each))
        for n in range(writers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)

    read = read_journal(journal)

    assert read.intact, read.damage
    assert len(read.events) == writers * each
    # every writer's events all arrived, and each is a whole line
    assert summarize(read.events).counts_by_type == {
        f"writer{n}": each for n in range(writers)
    }


def test_summarize(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    write_lines(
        journal,
        json.dumps({"ts": "2026-08-23T19:00:00.000Z", "type": "initialized"}),
        "{corrupt",
        json.dumps({"ts": "2026-08-23T20:00:00.000Z", "type": "observation"}),
        json.dumps({"ts": "2026-08-23T21:00:00.000Z", "type": "observation"}),
    )

    read = read_journal(journal)
    summary = summarize(read.events)

    # the fold sees what survived; the damage is reported separately rather
    # than silently shrinking the counts with no trace
    assert summary.count == 3
    assert summary.counts_by_type == {"initialized": 1, "observation": 2}
    assert summary.first_ts == "2026-08-23T19:00:00.000Z"
    assert summary.last_ts == "2026-08-23T21:00:00.000Z"
    assert summary.last is not None and summary.last.type == "observation"
    assert len(read.damage) == 1


# --- acknowledgments ----------------------------------------------------


def test_acks_fold_to_the_last_word_per_item(tmp_path: Path) -> None:
    # a re-acknowledgment carries the newer reason, which is the one that
    # describes why the item is currently silent
    journal = tmp_path / "journal.jsonl"
    append_event(journal, ACKNOWLEDGED, id="drift:abc", by="human", reason="first")
    append_event(journal, ACKNOWLEDGED, id="unreadable:x", by="agent", reason="other")
    append_event(journal, ACKNOWLEDGED, id="drift:abc", by="agent", reason="second")

    acks = read_acks(read_journal(journal).events)

    assert set(acks) == {"drift:abc", "unreadable:x"}
    assert acks["drift:abc"].reason == "second"
    assert acks["drift:abc"].by == "agent"
    assert acks["unreadable:x"].ts.startswith("20")


ACK_PAYLOADS = [
    ("no id at all", {"by": "human", "reason": "?"}),
    ("an id that is not a string", {"id": 7, "reason": "?"}),
    ("an empty id", {"id": "", "reason": "?"}),
]


@pytest.mark.parametrize(
    "payload",
    [payload for _, payload in ACK_PAYLOADS],
    ids=[case for case, _ in ACK_PAYLOADS],
)
def test_an_ack_this_version_cannot_use_is_data_rather_than_damage(
    payload: dict[str, object], tmp_path: Path
) -> None:
    # the same rule the vocabulary itself follows: a workspace outlives the
    # version that wrote it, so an unusable record is skipped, not raised on
    journal = tmp_path / "journal.jsonl"
    write_lines(journal, event_line(ACKNOWLEDGED, **payload))

    assert read_acks(read_journal(journal).events) == {}


def test_acks_of_nothing() -> None:
    assert read_acks([]) == {}


# --- positions, and the two folds keyed on them -------------------------


def test_a_position_is_the_line_it_was_read_from(tmp_path: Path) -> None:
    """Assigned by the reader, and counted over lines rather than over events.

    A damaged line keeps its number and stays counted, which is what makes a
    position stable: an index into `events` would silently renumber everything
    after the damage, so a cursor recorded before a torn append would come back
    pointing at the wrong entry.
    """
    journal = tmp_path / "journal.jsonl"
    write_lines(
        journal,
        event_line(PAUSED, by="human", reason="?"),
        "{not json at all",
        event_line(RESUMED),
    )

    read = read_journal(journal)

    assert [event.line for event in read.events] == [1, 3]
    assert read.damage[0].line == 2


def test_an_event_that_never_came_from_a_file_has_no_position() -> None:
    # a caller assembling one by hand is not naming a place in a journal, and a
    # `1` here would be a position pointing at somebody else's first line
    assert JournalEvent(ts="2026-08-23T19:00:00.000Z", type=RESUMED).line == 0


def test_a_position_does_not_leak_into_a_payload(tmp_path: Path) -> None:
    # every reader of an event iterates its payload; a key the writer never
    # wrote would show up in the journal summary and in `what happened`
    journal = tmp_path / "journal.jsonl"
    append_event(journal, COLLECTED, position=4)

    (event,) = read_journal(journal).events

    assert event.payload == {"position": 4}
    assert event.line == 1


def test_the_last_collection_is_the_one_in_force(tmp_path: Path) -> None:
    # a switch, like the pause: reaching backwards with `--since` and then
    # collecting again leaves the newer position behind
    journal = tmp_path / "journal.jsonl"
    append_event(journal, COLLECTED, position=12)
    append_event(journal, COLLECTED, position=40)

    collected = read_collected(read_journal(journal).events)

    assert collected is not None
    assert collected.position == 40
    assert collected.ts.startswith("20")


def test_never_collected_is_not_collected_at_zero() -> None:
    # *everything is new*, which is the right answer for a workspace no agent
    # has attached to, and not the same fact as a deliberate collection at 0
    assert read_collected([]) is None


def test_raising_folds_per_item_and_keeps_its_note(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    append_event(journal, RAISED, id="drift:abc", note="asked in #evals")
    append_event(journal, RAISED, id="stalled:t:2", note="")

    raised = read_raised(read_journal(journal).events)

    assert set(raised) == {"drift:abc", "stalled:t:2"}
    assert raised["drift:abc"].note == "asked in #evals"
    # optional where an ack's reason is required: handing a decision off does
    # not owe the account that disposing of one does
    assert raised["stalled:t:2"].note == ""


def test_a_hand_off_expires_once_a_turn_sees_the_item_gone(tmp_path: Path) -> None:
    # a park is keyed on its task, so a second approval hours later re-uses the
    # id of the one already answered. Without expiry the agent's queue would
    # set aside a decision nobody has been told about
    journal = tmp_path / "journal.jsonl"
    append_event(journal, OBSERVATION, items=["parked:alpha"])
    append_event(journal, RAISED, id="parked:alpha", note="asked in #evals")
    append_event(journal, OBSERVATION, items=["parked:alpha"])

    events = read_journal(journal).events
    assert set(read_raised(events)) == {"parked:alpha"}, "still parked, still raised"

    # answered: the item is gone from what the turn saw
    append_event(journal, OBSERVATION, items=[])
    # ...and a fresh park in the same task arrives at the same id
    append_event(journal, OBSERVATION, items=["parked:alpha"])

    assert read_raised(read_journal(journal).events) == {}


def test_an_observation_that_lists_nothing_expires_nothing(tmp_path: Path) -> None:
    # an older turn, or one this version cannot read the open set from, is not
    # a turn that saw no items -- treating it as one would clear the file
    journal = tmp_path / "journal.jsonl"
    append_event(journal, RAISED, id="drift:abc")
    append_event(journal, OBSERVATION, running=2)

    assert set(read_raised(read_journal(journal).events)) == {"drift:abc"}


def test_an_acknowledgment_outlives_the_condition_it_disposed_of(
    tmp_path: Path,
) -> None:
    # the asymmetry with raising, and the reason for it: *I told somebody* stops
    # being true of a condition that has been and gone, where *this is accepted*
    # stays true of the thing that was accepted
    journal = tmp_path / "journal.jsonl"
    append_event(journal, ACKNOWLEDGED, id="drift:abc", by="human", reason="on purpose")
    append_event(journal, OBSERVATION, items=[])

    assert set(read_acks(read_journal(journal).events)) == {"drift:abc"}


Fold = Callable[[list[JournalEvent]], object]

UNUSABLE: list[tuple[str, str, dict[str, object], Fold, object]] = [
    ("a collection with no position", COLLECTED, {}, read_collected, None),
    (
        "a collection positioned by text",
        COLLECTED,
        {"position": "4"},
        read_collected,
        None,
    ),
    ("a hand-off with no id", RAISED, {"note": "?"}, read_raised, {}),
    ("a hand-off with an empty id", RAISED, {"id": ""}, read_raised, {}),
]


@pytest.mark.parametrize(
    ("type", "payload", "fold", "expected"),
    [(type, payload, fold, expected) for _, type, payload, fold, expected in UNUSABLE],
    ids=[case for case, *_ in UNUSABLE],
)
def test_a_record_this_version_cannot_use_is_data_rather_than_damage(
    type: str,
    payload: dict[str, object],
    fold: Fold,
    expected: object,
    tmp_path: Path,
) -> None:
    # the rule the whole vocabulary follows: a workspace outlives the version
    # that wrote it, so an unusable record is skipped rather than raised on
    journal = tmp_path / "journal.jsonl"
    write_lines(journal, event_line(type, **payload))

    assert fold(read_journal(journal).events) == expected


def test_summarize_of_nothing() -> None:
    summary = summarize([])

    assert summary.count == 0
    assert summary.counts_by_type == {}
    assert summary.first_ts is None
    assert summary.last is None


def test_init_writes_a_readable_first_event(tmp_path: Path) -> None:
    # the journal marks the workspace, so the event init writes has to survive
    # the reader this step introduced
    workspace = create_workspace(tmp_path, git=False).workspace
    read = read_journal(workspace.journal)

    assert read.intact
    assert len(read.events) == 1
    event: JournalEvent = read.events[0]
    assert event.type == "initialized"
    assert event.payload == {"definition": "evalset.py"}


# --- two-state folds ----------------------------------------------------
#
# `paused` and `armed` are both *switches* rather than accumulations, which is
# what makes them different from `acknowledged`: the last word wins, and a
# double pause or a resume with nothing to resume is simply the state it leaves
# behind rather than an error somebody has to handle.


PAUSES: list[tuple[str, list[str], bool]] = [
    ("nothing", [], False),
    ("paused", [PAUSED], True),
    ("paused then resumed", [PAUSED, RESUMED], False),
    ("resumed then paused again", [PAUSED, RESUMED, PAUSED], True),
    ("paused twice", [PAUSED, PAUSED], True),
    ("resumed with nothing to resume", [RESUMED], False),
    ("resumed twice", [PAUSED, RESUMED, RESUMED], False),
]


@pytest.mark.parametrize(
    ("types", "expected"),
    [(types, expected) for _, types, expected in PAUSES],
    ids=[case for case, _, _ in PAUSES],
)
def test_the_last_word_decides_whether_a_run_is_paused(
    types: list[str], expected: bool, tmp_path: Path
) -> None:
    journal = tmp_path / "journal.jsonl"
    write_lines(
        journal,
        *(event_line(type, by="human", reason="because") for type in types),
    )

    assert (read_pause(read_journal(journal).events) is not None) is expected


def test_a_pause_carries_who_and_why(tmp_path: Path) -> None:
    # the only account of the decision that survives, and a later reader has to
    # be able to tell a deliberate hold from a forgotten one
    journal = tmp_path / "journal.jsonl"
    append_event(journal, PAUSED, by="agent", reason="the quota is exhausted")

    paused = read_pause(read_journal(journal).events)

    assert paused is not None
    assert (paused.by, paused.reason) == ("agent", "the quota is exhausted")
    assert paused.ts.endswith("Z")


def test_the_last_arming_is_the_one_in_force(tmp_path: Path) -> None:
    # re-arming at a new interval is the ordinary second reason to arm
    journal = tmp_path / "journal.jsonl"
    append_event(journal, ARMED, scheduler="cron", interval=600, label="steward-aaa")
    append_event(
        journal, ARMED, scheduler="launchd", interval=1800, label="steward-aaa"
    )

    armed = read_armed(read_journal(journal).events)

    assert armed is not None
    assert (armed.scheduler, armed.interval) == ("launchd", 1800)


def test_disarming_clears_it(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    append_event(journal, ARMED, scheduler="cron", interval=600, label="steward-aaa")
    append_event(journal, DISARMED, scheduler="cron")

    assert read_armed(read_journal(journal).events) is None


ARMED_PAYLOADS: list[tuple[str, dict[str, object]]] = [
    ("no scheduler", {"interval": 600}),
    ("no interval", {"scheduler": "cron"}),
    ("an interval that is not a number", {"scheduler": "cron", "interval": "10m"}),
]


@pytest.mark.parametrize(
    "payload",
    [payload for _, payload in ARMED_PAYLOADS],
    ids=[case for case, _ in ARMED_PAYLOADS],
)
def test_an_arming_this_version_cannot_use_is_data_rather_than_damage(
    payload: dict[str, object], tmp_path: Path
) -> None:
    journal = tmp_path / "journal.jsonl"
    write_lines(journal, event_line(ARMED, **payload))

    assert read_armed(read_journal(journal).events) is None
