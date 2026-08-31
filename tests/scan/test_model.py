"""`establish_scan_model`: the one mutation, in both directions.

The resolution *into* the declared value — flag over file over `STEWARD_SCAN_MODEL` — is `_settings`' work and tested with it; what is only true here is the reflexive contract with `SCOUT_SCAN_MODEL`: exporting settles it, `False` clears it, and silence reads it. The ambient spellings are cleared suite-wide (`no_ambient_channel`), so every case states its own environment.
"""

import os

from inspect_steward._scan import SCOUT_SCAN_MODEL, establish_scan_model
from pytest import MonkeyPatch


def test_a_declared_model_is_exported_for_workers_to_inherit() -> None:
    assert establish_scan_model("mockllm/model") == "mockllm/model"
    assert os.environ[SCOUT_SCAN_MODEL] == "mockllm/model"


def test_declining_clears_an_ambient_variable(monkeypatch: MonkeyPatch) -> None:
    """`false` is the one thing only Steward can say: the variable has no spelling for *not that*."""
    monkeypatch.setenv(SCOUT_SCAN_MODEL, "openai/expensive")
    assert establish_scan_model(False) is None
    assert SCOUT_SCAN_MODEL not in os.environ


def test_silence_defers_to_scouts_own_spelling(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(SCOUT_SCAN_MODEL, "mockllm/ambient")
    assert establish_scan_model(None) == "mockllm/ambient"
    assert os.environ[SCOUT_SCAN_MODEL] == "mockllm/ambient"


def test_a_declared_model_overwrites_a_differing_ambient_one(
    monkeypatch: MonkeyPatch,
) -> None:
    """The fleet agreeing with Steward matters more than which spelling was set first."""
    monkeypatch.setenv(SCOUT_SCAN_MODEL, "openai/other")
    assert establish_scan_model("mockllm/model") == "mockllm/model"
    assert os.environ[SCOUT_SCAN_MODEL] == "mockllm/model"


def test_nothing_configured_is_nothing_configured() -> None:
    assert establish_scan_model(None) is None
    assert SCOUT_SCAN_MODEL not in os.environ
