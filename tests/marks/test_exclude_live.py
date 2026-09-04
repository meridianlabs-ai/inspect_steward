"""An exclusion written by the real, detached runner.

The one claim the in-process tests cannot make: that a tend's spawn starts a process which finds the workspace, edits the landed log under the claim, journals what it wrote, and records its own exit — with nothing of it visible to the run's own fleet. One launch, one runner.
"""

import math
import shutil
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_steward._anomaly.applied import read_applied
from inspect_steward._launch import Launch, launch
from inspect_steward._marks import read_runs
from inspect_steward._marks.edit import EXCLUDED, STEWARD
from inspect_steward._signoff import UNWRITTEN, check
from inspect_steward._workspace import (
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
def test_an_exclusion_lands_in_the_log_from_a_detached_runner(
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
    clean = {
        str(sample.id): (sample.uuid, (sample.scores or {})["exact"].value)
        for sample in (before.samples or [])
        if sample.error is None
    }
    assert len(errored) == 2 and len(clean) == 2
    (window,) = observed.anomalies.open
    inflight_before = workspace.inflight.read_text(encoding="utf-8")

    decision: dict[str, Any] = {
        "class": window.class_key,
        "disposition": "exclude",
        "reason": "the provider was down; these are not coming back",
        "by": "kaia",
        "effect": "2 samples excluded from scoring",
    }
    append_event(workspace.journal, RULING, **decision)

    # the turn starts the runner and reports the samples as not yet written...
    spawned = turn(workspace)
    assert spawned.dispositions.pending == {window.class_key: 2}
    assert UNWRITTEN in [blocker.kind for blocker in check(spawned, None)]
    (run,) = read_runs(workspace.marks_runs)

    # ...and the runner, on its own, lands it
    until(
        "the runner to journal the exclusion",
        lambda: bool(
            read_applied(read_journal(workspace.journal).events).edited_uuids(
                window.class_key, _ruled_at(workspace)
            )
        ),
    )
    ended = read_runs(workspace.marks_runs)[run]
    assert ended.exited and ended.status == 0, ended.detail

    after = read_eval_log(first.name)
    by_id = {str(sample.id): sample for sample in (after.samples or [])}
    for sample_id, uuid in errored.items():
        score = (by_id[sample_id].scores or {})["exact"]
        assert by_id[sample_id].uuid == uuid
        assert math.isnan(score.as_float())
        assert score.reason == EXCLUDED
        assert score.history[-1].provenance is not None
        assert score.history[-1].provenance.metadata[STEWARD]["class"] == (
            window.class_key
        )
    for sample_id, (uuid, value) in clean.items():
        assert by_id[sample_id].uuid == uuid
        assert (by_id[sample_id].scores or {})["exact"].value == value
        assert (by_id[sample_id].scores or {})["exact"].history == []
    assert after.results is not None and after.results.completed_samples == 2

    # nothing of it reached the run: one log, the same in-flight record
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
