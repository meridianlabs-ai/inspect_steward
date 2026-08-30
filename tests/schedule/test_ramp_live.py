"""The ramp climbing on a real worker — the one direction a live test can drive.

`mockllm` never answers 429, so every window is clean and the run *should* climb: launch at the default floor, hold the limiter saturated (every sample parks on an approval, borrowing step 20's mechanism as load), and watch a tend buy the first step. The down path — the storm cut, the stepwise restore — can never be exercised this way and lives entirely in `test_tuning.py`'s tables (design/testing.md).

What the one launch buys is the chain no synthesized state can vouch for: the task-config read off the live socket, the gates against a real journal's baselines, `inspect ctl` accepting the retune, the eval's `ResizableLimiter` actually resizing, and the journal `action` whose fold is what a respawn would start from.

**Budget: one launch**, and a handful of tends against it.
"""

from pathlib import Path
from typing import Any

import pytest
from inspect_steward._launch import Launch, launch
from inspect_steward._tend import Refused, TendResult, tend
from inspect_steward._workspace import (
    ACTION,
    Workspace,
    create_workspace,
    read_journal,
)

from .._fault import until
from ..timer._fake import clear_credentials, fake_cron

FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"
DEFINITION = "ramp_evalset.py"

FLOOR = 40
"""The default ramp's floor, which is where the worker must start."""


def ramp_actions(workspace: Workspace) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == ACTION and event.payload.get("action") == "ramp"
    ]


def turn(workspace: Workspace) -> TendResult:
    result = tend(workspace)
    assert not isinstance(result, Refused), "nothing else holds this claim"
    return result


def test_a_saturated_worker_with_no_pushback_earns_a_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cron(monkeypatch)
    clear_credentials(monkeypatch)

    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    definition = workspace.root / DEFINITION
    definition.write_bytes((FIXTURES / DEFINITION).read_bytes())

    started = launch(workspace, definition)
    assert isinstance(started, Launch), f"refused by {started}"
    assert started.turn is not None and len(started.turn.spawned) == 1

    # a state to wait for rather than a delay to outlast: forty-five samples
    # park, so the limiter sits at 40/40 with a queue -- demand, held steady
    def saturated() -> bool:
        row = next(iter(turn(workspace).progress.rows), None)
        return row is not None and row.running >= FLOOR

    until("the sample limiter to saturate", saturated)

    # tends until the window is whole: the first live read is the baseline, the
    # next clean window buys the step -- each turn here is the real loop, gates
    # and all, against the journal the previous one wrote
    def stepped() -> bool:
        turn(workspace)
        return any(
            payload.get("knob") == "max_samples" for payload in ramp_actions(workspace)
        )

    until("the ramp to buy its first step", stepped)

    (step,) = [
        payload
        for payload in ramp_actions(workspace)
        if payload.get("knob") == "max_samples"
    ]
    assert (step.get("at"), step.get("to")) == (FLOOR, FLOOR + 20)
    assert "authorized range" in str(step.get("reason"))

    # the retune landed on the worker, not only in the record: the next live
    # read reports the raised limit, and the freed slots admit parked samples
    def landed() -> bool:
        levels: dict[str, int] = turn(workspace).tuning.record.get("levels", {})
        return FLOOR + 20 in levels.values()

    until("the raised limit to read back live", landed)

    # the connection ceiling was patched toward the ramp target where the
    # worker had adaptive controllers to patch; absent controllers, no move
    for payload in ramp_actions(workspace):
        if payload.get("knob") == "max_connections":
            assert payload.get("to") == 200
