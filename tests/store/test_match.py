"""`satisfied` — the read half of rung 2, shared by the launch and the rehearsal.

One predicate for both callers, so a smoke cannot skip a task the launch then runs. Layer 1: a directory store of synthesized logs, no processes.
"""

import importlib
from pathlib import Path

import pytest
from inspect_steward._store import StoreError, satisfied

from .._logs import SynthTask, synth_manifest, write_log

ADDITION = SynthTask("addition", samples=2)
ECHO = SynthTask("echo", samples=2)


def ask(tmp_path: Path, *tasks: SynthTask, notes: list[str]) -> dict[str, str]:
    return satisfied(
        synth_manifest([ADDITION, ECHO]),
        {task.identifier for task in tasks},
        str(tmp_path / "store"),
        root=tmp_path,
        log=notes.append,
    )


def test_a_log_that_answers_is_claimed_and_a_short_one_is_not(
    tmp_path: Path,
) -> None:
    # same identifier twice: one ran the whole task, one ran half of it. The
    # store ranks by size, so the short one is behind — and every candidate is
    # asked rather than only the front of the list
    store = tmp_path / "store"
    short = write_log(store, ADDITION, total=1, completed=1)
    full = write_log(store, ADDITION, created="2026-01-02T00:00:00")
    notes: list[str] = []

    found = ask(tmp_path, ADDITION, ECHO, notes=notes)

    # the source is the store's own name for the log, a `file://` URI here
    assert set(found) == {ADDITION.identifier}
    assert found[ADDITION.identifier].endswith(full.name)
    assert not found[ADDITION.identifier].endswith(short.name)


def test_a_task_only_a_short_log_answers_is_not_claimed_and_is_noted(
    tmp_path: Path,
) -> None:
    write_log(tmp_path / "store", ADDITION, total=1, completed=1)
    notes: list[str] = []

    assert ask(tmp_path, ADDITION, notes=notes) == {}
    assert any("does not answer what this run asks" in one for one in notes)


def test_a_candidate_that_will_not_read_is_skipped_and_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(tmp_path / "store", ADDITION)
    module = importlib.import_module("inspect_steward._store.match")

    def refuse(source: str) -> object:
        raise RuntimeError("the credentials expired")

    monkeypatch.setattr(module, "read_attempt", refuse)
    notes: list[str] = []

    assert ask(tmp_path, ADDITION, notes=notes) == {}
    assert any("would not read from the store" in one for one in notes)


def test_an_empty_store_answers_nothing(tmp_path: Path) -> None:
    (tmp_path / "store").mkdir()
    assert ask(tmp_path, ADDITION, notes=[]) == {}


def test_nothing_wanted_asks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("inspect_steward._store.match")

    def refuse(location: str, *, root: Path) -> object:
        raise AssertionError("the store was opened for an empty question")

    monkeypatch.setattr(module, "open_store", refuse)
    assert ask(tmp_path, notes=[]) == {}


def test_a_store_that_will_not_open_is_the_callers_to_explain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("inspect_steward._store.match")

    def refuse(location: str, *, root: Path) -> object:
        raise StoreError("the bucket is not there")

    monkeypatch.setattr(module, "open_store", refuse)
    with pytest.raises(StoreError):
        ask(tmp_path, ADDITION, notes=[])
