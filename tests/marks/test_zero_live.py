"""A zero obtained from the task's own scorer, in a scratch side run.

The claim no synthesized log can make: that the runner spawns the definition on just the ruled sample ids into `.steward/marks/`, cancel-scores what it starts, and copies the scorer's verdict into the main log with history and provenance — and that nothing of the side run reaches `logs/` or the run's in-flight record. One launch, one runner, one side worker.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_steward._anomaly.applied import read_applied
from inspect_steward._launch import Launch, launch
from inspect_steward._marks import read_runs
from inspect_steward._marks.edit import STEWARD, ZEROED
from inspect_steward._signoff import UNWRITTEN, check
from inspect_steward._workspace import (
    ACTION,
    RULING,
    Workspace,
    append_event,
    create_workspace,
    read_journal,
)

from .._fault import until
from ..schedule.test_tend import settle, turn
from ..timer._fake import clear_credentials, fake_cron

FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"


@pytest.mark.runner
def test_a_zero_is_the_scorers_verdict_on_an_empty_attempt(
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

    started = launch(workspace, definition)
    assert isinstance(started, Launch), f"refused by {started}"
    settle(workspace)
    observed = turn(workspace)
    (first,) = list_eval_logs(str(workspace.logs))
    before = read_eval_log(first.name)
    errored = {
        str(sample.id): sample.uuid
        for sample in (before.samples or [])
        if sample.error is not None
    }
    assert len(errored) == 2
    (window,) = observed.anomalies.open
    inflight_before = workspace.inflight.read_text(encoding="utf-8")

    # the outage passes, but the operator wants these counted as failures
    # rather than run again -- which needs the scorer's word for a failure
    (markers / "healed").touch()
    decision: dict[str, Any] = {
        "class": window.class_key,
        "disposition": "zero",
        "reason": "the model had its chance",
        "by": "kaia",
        "effect": "2 samples scored zero",
    }
    append_event(workspace.journal, RULING, **decision)

    spawned = turn(workspace)
    assert spawned.dispositions.pending == {window.class_key: 2}
    (run,) = read_runs(workspace.marks_runs)

    until(
        "the side run to land and the runner to journal the zero",
        lambda: bool(
            read_applied(read_journal(workspace.journal).events).edited_uuids(
                window.class_key, _ruled_at(workspace)
            )
        ),
        timeout=300,
    )
    ended = read_runs(workspace.marks_runs)[run]
    assert ended.exited and ended.status == 0, ended.detail

    # the side run left its logs in the run's own scratch directory
    (event,) = [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == ACTION and event.payload.get("run") == run
    ]
    side = event["side_run"]
    assert Path(side["log_dir"]).is_relative_to(workspace.marks_run(run))
    assert len(list_eval_logs(side["log_dir"])) == 1
    scratch = read_eval_log(list_eval_logs(side["log_dir"])[0].name)
    assert {str(sample.id) for sample in scratch.samples or []} == set(errored)

    # and the main log's samples carry the scorer's verdict, transcript intact
    after = read_eval_log(first.name)
    by_id = {str(sample.id): sample for sample in (after.samples or [])}
    for sample_id, uuid in errored.items():
        sample = by_id[sample_id]
        assert sample.uuid == uuid
        assert sample.error is not None
        score = (sample.scores or {})["exact"]
        assert score.value == "I"
        assert score.reason == ZEROED
        assert score.history[-1].provenance is not None
        assert score.history[-1].provenance.author == "kaia"
        assert score.history[-1].provenance.metadata[STEWARD]["disposition"] == "zero"

    assert len(list_eval_logs(str(workspace.logs))) == 1
    assert workspace.inflight.read_text(encoding="utf-8") == inflight_before
    settled = turn(workspace)
    assert settled.dispositions.pending == {}
    assert UNWRITTEN not in [blocker.kind for blocker in check(settled, None)]
    assert settled.failures == []


def _ruled_at(workspace: Workspace) -> str:
    return [
        event.ts
        for event in read_journal(workspace.journal).events
        if event.type == RULING
    ][-1]
