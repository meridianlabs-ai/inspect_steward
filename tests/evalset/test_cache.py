"""Skipping header reads without ever skipping a change.

An accelerator that is wrong is worse than no accelerator, and the way this one
would be wrong is by serving a stale answer for a file that moved underneath
it. So the cases that matter are the three mutations a log can undergo — it
grows while running, it is rewritten by an invalidation, it leaves the
directory — and the claim is that each one is noticed.

No processes and no evals: every log here is a file `tests/_logs.py` wrote.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from inspect_steward._evalset.cache import (
    CACHE_VERSION,
    AttemptCache,
    read_attempt_cache,
    write_attempt_cache,
)
from inspect_steward._evalset.observe import observe_logs

from .._logs import SynthTask, write_log, write_unreadable

TASK = SynthTask("probe", samples=10, epochs=1)


def stamp(path: Path, *, at: str) -> None:
    when = datetime.fromisoformat(at).timestamp()
    os.utime(path, (when, when))


def test_the_second_read_of_a_settled_directory_reads_nothing(tmp_path: Path) -> None:
    for n in range(5):
        write_log(tmp_path, SynthTask(f"task{n}"))
    cache = AttemptCache()

    first = observe_logs(tmp_path, cache=cache)
    hits_after_first = cache.hits
    second = observe_logs(tmp_path, cache=cache)

    assert hits_after_first == 0
    assert cache.hits == 5
    # and the point of the exercise: the answer is the same either way
    assert second == first


def test_a_running_log_is_never_cached(tmp_path: Path) -> None:
    # it changes constantly, and it is the one file where a rewrite that
    # preserved both the size and the mtime second would go unnoticed. Excluding
    # it costs a handful of reads against a directory of thousands
    write_log(tmp_path, TASK, status="started")
    cache = AttemptCache()

    observe_logs(tmp_path, cache=cache)
    observe_logs(tmp_path, cache=cache)

    assert cache.entries == {}
    assert cache.hits == 0


def test_a_log_that_grew_is_read_again(tmp_path: Path) -> None:
    written = write_log(tmp_path, TASK, total=10, completed=3)
    cache = AttemptCache()
    observe_logs(tmp_path, cache=cache)

    # the same task, further along -- a resume rewriting its log in place
    write_log(tmp_path, TASK, total=10, completed=9)
    observed = observe_logs(tmp_path, cache=cache)

    assert written.exists()
    current = observed.current(TASK.identifier)
    assert current is not None and current.completed_samples == 9


def test_an_invalidation_is_noticed(tmp_path: Path) -> None:
    # the mutation that would be silent if the key were size alone: marking
    # samples invalid rewrites the log at very nearly the same length. It is
    # also the mutation `_stalled` dates from mtime, so a cache that missed it
    # would put the two readers of that timestamp into disagreement
    written = write_log(tmp_path, TASK, total=10, completed=10)
    stamp(written, at="2026-08-23T10:00:00+00:00")
    cache = AttemptCache()
    observe_logs(tmp_path, cache=cache)

    written.unlink()
    rewritten = write_log(tmp_path, TASK, total=10, completed=10, invalidated=True)
    stamp(rewritten, at="2026-08-23T21:00:00+00:00")
    observed = observe_logs(tmp_path, cache=cache)

    current = observed.current(TASK.identifier)
    assert current is not None
    assert current.invalidated is True
    assert current.mtime == pytest.approx(
        datetime(2026, 8, 23, 21, tzinfo=timezone.utc).timestamp() * 1000
    )


def test_an_archived_log_stops_being_remembered(tmp_path: Path) -> None:
    # how it stays bounded without a policy: a log leaves the directory, stops
    # being offered, and its entry goes with it on the next write
    keep = write_log(tmp_path, TASK)
    gone = write_log(tmp_path, SynthTask("removed"))
    cache = AttemptCache()
    observed = observe_logs(tmp_path, cache=cache)
    assert len(cache.entries) == 2

    gone.unlink()
    observed = observe_logs(tmp_path, cache=cache)
    narrowed = cache.keep({*observed.locations})

    assert [Path(location).name for location in narrowed.entries] == [keep.name]


def test_an_unreadable_file_is_never_cached_as_an_answer(tmp_path: Path) -> None:
    # a zip half-written by a starting worker is an ordinary transient, and
    # caching "this is not a log" would make it one forever
    write_unreadable(tmp_path)
    cache = AttemptCache()

    observed = observe_logs(tmp_path, cache=cache)

    assert len(observed.unreadable) == 1
    assert cache.entries == {}


def test_a_cache_survives_the_round_trip(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    for n in range(3):
        write_log(logs, SynthTask(f"task{n}"))
    written = AttemptCache()
    observe_logs(logs, cache=written)
    path = tmp_path / "state" / "observed.json"

    write_attempt_cache(path, written)
    restored = read_attempt_cache(path)

    assert restored.entries == written.entries
    # and it is usable rather than merely equal
    observe_logs(logs, cache=restored)
    assert restored.hits == 3


DISCARDED: list[tuple[str, str]] = [
    ("not json at all", "{"),
    ("an empty file", ""),
    ("not an object", "[]"),
    ("a version this Steward does not read", '{"version": 99, "logs": {}}'),
    ("no logs key", '{"version": 1}'),
    ("logs is not a mapping", '{"version": 1, "logs": []}'),
]


@pytest.mark.parametrize(
    "content",
    [content for _, content in DISCARDED],
    ids=[case for case, _ in DISCARDED],
)
def test_a_cache_that_cannot_be_trusted_is_simply_empty(
    content: str, tmp_path: Path
) -> None:
    # nothing here may raise: every caller is on the fast path of a turn, and a
    # crash over an accelerator would cost the thing it was accelerating
    path = tmp_path / "observed.json"
    path.write_text(content, encoding="utf-8")

    assert read_attempt_cache(path).entries == {}


def test_a_missing_cache_is_an_empty_one(tmp_path: Path) -> None:
    assert read_attempt_cache(tmp_path / "never-written.json").entries == {}


def test_one_damaged_record_costs_one_header_read(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    write_log(logs, TASK)
    write_log(logs, SynthTask("other"))
    cache = AttemptCache()
    observe_logs(logs, cache=cache)
    path = tmp_path / "observed.json"
    write_attempt_cache(path, cache)

    # a field from a `LogAttempt` this version has never heard of, which is what
    # a cache written by a later Steward looks like record by record
    document = path.read_text(encoding="utf-8").replace(
        '"attempt": {', '"attempt": {"invented_later": 1, ', 1
    )
    path.write_text(document, encoding="utf-8")

    restored = read_attempt_cache(path)

    # the survivor is still usable rather than the whole file being thrown away
    assert len(restored.entries) == 1


def test_an_unwritable_destination_is_swallowed(tmp_path: Path) -> None:
    blocked = tmp_path / "wall"
    blocked.write_text("not a directory", encoding="utf-8")

    write_attempt_cache(blocked / "observed.json", AttemptCache())


def test_the_version_is_written(tmp_path: Path) -> None:
    # a reader from another version discards rather than migrates, which only
    # works if the writer says which version it was
    import json

    path = tmp_path / "observed.json"
    write_attempt_cache(path, AttemptCache())

    assert json.loads(path.read_text(encoding="utf-8"))["version"] == CACHE_VERSION
