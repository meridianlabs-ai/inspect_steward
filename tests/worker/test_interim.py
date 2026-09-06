"""The interim harvest across turns.

A cache that is wrong costs a stale number, so the cases are the ones every other cache under `.steward/` answers: a round trip, another version, a file that will not read, and an entry that does not parse.
"""

import json
from pathlib import Path

from inspect_steward._worker import (
    INTERIM_VERSION,
    Interim,
    InterimEntry,
    read_interim,
    write_interim,
)

KNOWN = {
    "E1": Interim(
        scored=3,
        entries=(
            InterimEntry(name="exact", reducer=None, metrics={"accuracy": 0.5}),
            InterimEntry(name="judge", reducer="pass_at_5", metrics={"mean": 0.4}),
        ),
    ),
    "E2": Interim(scored=1),
}


def test_the_harvest_round_trips(tmp_path: Path) -> None:
    path = tmp_path / ".steward" / "interim.json"

    write_interim(path, KNOWN)

    assert read_interim(path) == KNOWN


def test_another_version_is_discarded(tmp_path: Path) -> None:
    path = tmp_path / "interim.json"
    write_interim(path, KNOWN)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["version"] = INTERIM_VERSION + 1
    path.write_text(json.dumps(document), encoding="utf-8")

    assert read_interim(path) == {}


def test_a_file_that_will_not_read_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "interim.json"
    assert read_interim(path) == {}
    path.write_text("{not json", encoding="utf-8")
    assert read_interim(path) == {}


def test_an_entry_that_does_not_parse_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "interim.json"
    path.write_text(
        json.dumps(
            {
                "version": INTERIM_VERSION,
                "evals": {
                    "E1": {"scored": "three", "entries": []},
                    "E2": {"scored": 2, "entries": [{"name": "exact"}, 7]},
                },
            }
        ),
        encoding="utf-8",
    )

    assert read_interim(path) == {"E2": Interim(scored=2)}
