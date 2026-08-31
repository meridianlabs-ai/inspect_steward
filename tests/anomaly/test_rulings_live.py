"""The invalidate-and-resume cycle, end to end against real workers.

The one claim no synthesized state can make: that a `rerun` ruling actually buys a re-run of exactly the ruled samples — the landed log invalidated with the ruling's provenance, the respawn scheduled first, the clean samples reused byte-for-byte (same uuid), the ruled ones run fresh, and the window resolving `reran_passed` when the re-run comes home clean.

**Budget: one launch, two workers** — the erroring run and its authorized re-run. The warm half stays offline by design (`test_rulings.py`); no eval is reliably mid-run on demand.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_steward._anomaly.model import AnomalyState, Outcome
from inspect_steward._launch import Launch, launch
from inspect_steward._workspace import RULING, Workspace, append_event, create_workspace

from ..schedule.test_tend import settle, turn
from ..timer._fake import clear_credentials, fake_cron

FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"


def test_a_rerun_ruling_reruns_the_ruled_samples_and_only_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cron(monkeypatch)
    clear_credentials(monkeypatch)
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("ERRORING_EVALSET_DIR", str(markers))
    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    definition = workspace.root / "evalset.py"
    shutil.copy(FIXTURES / "erroring_evalset.py", definition)

    # the outage: the run completes "successfully" with two errored samples
    started = launch(workspace, definition)
    assert isinstance(started, Launch), f"refused by {started}"
    settle(workspace)
    observed = turn(workspace)
    (first,) = list_eval_logs(str(workspace.logs))
    before = read_eval_log(first.name)
    assert before.status == "success"
    errored = {
        str(sample.id): sample.uuid
        for sample in (before.samples or [])
        if sample.error is not None
    }
    clean = {
        str(sample.id): sample.uuid
        for sample in (before.samples or [])
        if sample.error is None
    }
    assert len(errored) == 2 and len(clean) == 2
    (window,) = observed.anomalies.open
    assert window.kind == "error"

    # the ruling, made after the provider "recovers"
    (markers / "healed").touch()
    decision: dict[str, Any] = {
        "class": window.class_key,
        "disposition": "rerun",
        "reason": "the outage passed",
        "by": "kaia",
    }
    append_event(workspace.journal, RULING, **decision)

    # one turn invalidates the landed log with the ruling's provenance...
    applied = turn(workspace)
    invalidated = read_eval_log(first.name)
    assert invalidated.invalidated is True
    marked = [
        sample
        for sample in (invalidated.samples or [])
        if sample.invalidation is not None
    ]
    assert {str(sample.id) for sample in marked} == set(errored)
    assert {sample.invalidation.author for sample in marked if sample.invalidation} == {
        "kaia"
    }
    assert applied.summary.stalled == []

    # ...and the next respawns it as an authorized re-run
    respawned = turn(workspace)
    assert len(respawned.spawned) == 1
    assert respawned.summary.rerunning == 1
    settle(workspace)
    finished = turn(workspace)

    # the re-run reused the clean samples and re-ran only the ruled ones
    current = max(
        (read_eval_log(log.name) for log in list_eval_logs(str(workspace.logs))),
        key=lambda log: log.eval.created,
    )
    assert current.status == "success"
    after = {str(sample.id): sample for sample in (current.samples or [])}
    assert {id: after[id].uuid for id in clean} == clean
    for id in errored:
        assert after[id].error is None
        assert after[id].uuid != errored[id]

    # and the window resolved: the re-run passed, nothing is open
    assert finished.anomalies.open == ()
    (settled,) = finished.anomalies.settled
    assert settled.state is AnomalyState.RESOLVED
    assert settled.resolution is not None
    assert settled.resolution.outcome is Outcome.RERAN_PASSED
