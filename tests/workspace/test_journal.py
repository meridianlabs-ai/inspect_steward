"""The journal, and mostly what it does when something has gone wrong.

The ordinary path is one line of JSON. What earns the tests is the rest: a
crash mid-append, a line that was never valid, an event type written by a
version that does not exist yet, and two processes appending at once. Each of
those is a claim the design makes, and none is visible on a run that goes well.
"""

import json
import multiprocessing
from pathlib import Path

import pytest
from inspect_steward._workspace import (
    JournalEvent,
    append_event,
    create_workspace,
    read_journal,
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
