"""Carrying a marking ruling out: the executor, the record, and the runner in-process.

The suite stands in for the runner's launch (`conftest.py`), so a turn records a run and starts nothing; `apply_marks` then runs it here. What that buys is every claim about the *tend's* side — one run per ruling, none while one is running, a bounded retry, deferral around live workers — without a process in the loop, and the runner's own body exercised over a real log rather than a mock of one. The real, detached runner is the live tests' claim (`test_exclude_live.py`, `test_zero_live.py`).
"""

import math
from pathlib import Path
from typing import Any, cast

import pytest
from inspect_ai.log import read_eval_log
from inspect_steward._anomaly.applied import RULING_APPLIED, read_applied
from inspect_steward._anomaly.model import Anomalies, AnomalyState, Disposition
from inspect_steward._evalset.instances import Instance
from inspect_steward._evalset.observe import ObservedTasks, TaskObservation, TaskState
from inspect_steward._marks import MARK_ATTEMPTS, Target, read_runs, resolve_runs
from inspect_steward._marks.edit import EXCLUDED
from inspect_steward._marks.state import (
    record_exited,
    record_intent,
    record_launched,
)
from inspect_steward._scan import sync_scan
from inspect_steward._signoff import FAILED, UNWRITTEN, check
from inspect_steward._workspace import (
    ACTION,
    PAUSED,
    RESUMED,
    JournalEvent,
    Workspace,
    append_event,
    read_journal,
)

from .._logs import SynthSample, write_log
from ..anomaly.test_items import CLASS, erroring, ruling
from ..anomaly.test_rulings import (
    IDENT,
    apply,
    attempt,
    census,
    inst,
    over,
    workspace_at,
)
from ..anomaly.test_scan_items import SCAN_ID, TASK, record, rule, scanning
from ..schedule.test_tend import turn
from ._runner import apply_marks


def applications(workspace: Workspace) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == ACTION and event.payload.get("action") == RULING_APPLIED
    ]


def edited(event: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], event["edited"])


# --- through real turns ----------------------------------------------------


def test_an_exclusion_is_one_run_and_the_next_turn_finds_it_written(
    tmp_path: Path,
) -> None:
    workspace = erroring(tmp_path, errors=2, samples=4)
    turn(workspace)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")

    started = turn(workspace)

    # the turn recorded one run addressing the two ruled samples, and reports
    # both as not yet written -- applied is a state, and it is not yet reached
    (run,) = read_runs(workspace.marks_runs).values()
    assert run.disposition is Disposition.EXCLUDE
    assert sorted(target.sample_id for target in run.targets) == ["s0", "s1"]
    assert started.dispositions.pending == {CLASS: 2}
    assert UNWRITTEN in [blocker.kind for blocker in check(started, None)]

    applied = apply_marks(workspace)
    settled = turn(workspace)

    assert applied == [run.run]
    (event,) = applications(workspace)
    assert event["for"] == run.ruling_ts and event["run"] == run.run
    (entry,) = edited(event)
    assert sorted(entry["uuids"]) == ["uuid-s0-1", "uuid-s1-1"]
    assert settled.dispositions.pending == {}
    assert UNWRITTEN not in [blocker.kind for blocker in check(settled, None)]
    # and no second run: the remainder is empty
    assert len(read_runs(workspace.marks_runs)) == 1
    log = read_eval_log(entry["location"])
    excluded = {
        str(sample.id): (sample.scores or {})["exact"]
        for sample in log.samples or []
        if sample.error is not None
    }
    assert all(math.isnan(score.as_float()) for score in excluded.values())
    assert {score.reason for score in excluded.values()} == {EXCLUDED}


def test_a_run_still_running_is_not_started_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = erroring(tmp_path)
    turn(workspace)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")
    turn(workspace)
    (run,) = read_runs(workspace.marks_runs).values()
    # the stood-in launch records pid 0; a scan that finds it carrying the run
    # id is a runner still going
    monkeypatch.setattr("inspect_steward._marks.state.scan_runs", lambda: {0: run.run})

    turn(workspace)

    assert len(read_runs(workspace.marks_runs)) == 1


def test_a_run_that_ends_without_writing_is_retried_then_reported(
    tmp_path: Path,
) -> None:
    """Three attempts, then the turn says it could not, every turn until somebody rules afresh."""
    workspace = erroring(tmp_path)
    turn(workspace)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")

    for expected in range(1, MARK_ATTEMPTS + 1):
        result = turn(workspace)
        runs = list(read_runs(workspace.marks_runs))
        assert len(runs) == expected
        assert runs[-1].endswith(f"-{expected}")
        assert result.failures == []

    given_up = turn(workspace)

    assert len(read_runs(workspace.marks_runs)) == MARK_ATTEMPTS
    (failure,) = given_up.failures
    assert "could not write the exclude on openai.APITimeoutError errors" in failure
    assert f"failed {MARK_ATTEMPTS} times" in failure
    assert "run.log" in failure
    assert FAILED in [blocker.kind for blocker in check(given_up, None)]


def test_a_zero_is_held_while_the_run_is_paused(tmp_path: Path) -> None:
    # pausing means no new workers, and a zero's side run starts some. An
    # exclusion writes a file and proceeds, as a re-run's invalidation does
    workspace = erroring(tmp_path)
    turn(workspace)
    append_event(workspace.journal, PAUSED, by="test", reason="hold")
    ruling(workspace, "zero", effect="2 samples scored zero")

    turn(workspace)
    assert read_runs(workspace.marks_runs) == {}

    append_event(workspace.journal, RESUMED)
    turn(workspace)

    (run,) = read_runs(workspace.marks_runs).values()
    assert run.disposition is Disposition.ZERO


def test_the_runner_finds_nothing_left_after_a_crash_between_write_and_record(
    tmp_path: Path,
) -> None:
    """The log is the witness: a second run over a written log books it without editing twice."""
    workspace = erroring(tmp_path)
    turn(workspace)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")
    turn(workspace)
    apply_marks(workspace)
    # the crash: the effect landed, the journal line did not
    journal = workspace.journal.read_text(encoding="utf-8").splitlines()
    workspace.journal.write_text(
        "\n".join(line for line in journal if RULING_APPLIED not in line) + "\n",
        encoding="utf-8",
    )

    turn(workspace)
    apply_marks(workspace)

    (event,) = applications(workspace)
    (entry,) = edited(event)
    assert entry["found"] == 2
    assert sorted(entry["uuids"]) == ["uuid-s0-1", "uuid-s1-1"]
    log = read_eval_log(entry["location"])
    histories = [
        len((sample.scores or {})["exact"].history)
        for sample in log.samples or []
        if sample.error is not None
    ]
    assert histories == [1, 1]


def test_a_scan_finding_is_written_into_the_sample_its_row_names(
    tmp_path: Path,
) -> None:
    """A scan instance is addressed by the row's uuid, and the log is opened at the task's current attempt."""
    workspace = scanning(tmp_path, land=False)
    samples = [
        SynthSample("s1", score=1.0),
        SynthSample("s2", score=1.0),
        SynthSample("s3", score=0.0),
        SynthSample("s4", score=0.0),
    ]
    log = write_log(
        workspace.logs, TASK, samples=samples, scores={"exact": {"accuracy": 0.5}}
    )
    record(
        workspace,
        str(log),
        uuid=samples[0].uuid,
        sample_id="s1",
        value=True,
        label="reward_hacking",
    )
    record(
        workspace,
        str(log),
        uuid=samples[3].uuid,
        sample_id="s4",
        value=False,
        label=None,
    )
    sync_scan(log_dir=str(workspace.logs), scan_id=SCAN_ID)
    turn(workspace)
    rule(workspace, "exclude", effect="1 sample is out of the scores")

    turn(workspace)
    apply_marks(workspace)
    settled = turn(workspace)

    after = read_eval_log(str(log))
    scores = {
        str(sample.id): (sample.scores or {})["exact"] for sample in after.samples or []
    }
    assert math.isnan(scores["s1"].as_float()) and scores["s1"].reason == EXCLUDED
    assert scores["s1"].history[0].value == 1.0
    assert all(scores[other].history == [] for other in ("s2", "s3", "s4"))
    # the headline moved by itself: one of three remaining is correct
    assert after.results is not None
    assert after.results.scores[0].metrics["accuracy"].value == pytest.approx(1 / 3)
    assert settled.dispositions.pending == {}
    assert settled.dispositions.excluded == 1


# --- the executor, driven directly -----------------------------------------


def excluded(*instances: Instance) -> Anomalies:
    return Anomalies(
        settled=(
            over(
                *instances,
                state=AnomalyState.ACCEPTED,
                disposition=Disposition.EXCLUDE,
            ),
        )
    )


def landed() -> ObservedTasks:
    return ObservedTasks(
        tasks=[
            TaskObservation(
                identifier=IDENT, state=TaskState.COMPLETE, task=None, current=attempt()
            )
        ]
    )


def test_a_task_with_a_live_worker_defers_the_run(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path)
    sample = inst("s1")

    apply(
        workspace,
        census(sample),
        anomalies=excluded(sample),
        running=(IDENT,),
        observed=landed(),
    )

    assert read_runs(workspace.marks_runs) == {}


def test_a_task_just_spawned_for_defers_the_run(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path)
    sample = inst("s1")

    apply(
        workspace,
        census(sample),
        anomalies=excluded(sample),
        running=(),
        spawned={IDENT},
        observed=landed(),
    )

    assert read_runs(workspace.marks_runs) == {}


def test_a_landed_task_gets_a_run_addressed_to_its_current_log(
    tmp_path: Path,
) -> None:
    workspace = workspace_at(tmp_path)
    sample = inst("s1")

    apply(
        workspace,
        census(sample),
        anomalies=excluded(sample),
        running=(),
        observed=landed(),
    )

    (run,) = read_runs(workspace.marks_runs).values()
    assert run.targets == (
        Target(
            task=IDENT,
            location="logs/a.eval",
            eval_id="e1",
            sample_id="s1",
            epoch=1,
            uuid="u-s1",
        ),
    )


def test_an_instance_no_longer_in_the_results_is_not_a_target(
    tmp_path: Path,
) -> None:
    # the census still carries an instance from a superseded attempt; a runner
    # sent after it would fail to find it every turn until the budget ran out
    workspace = workspace_at(tmp_path)
    sample = inst("s1")

    apply(
        workspace,
        census(sample),
        anomalies=excluded(sample),
        running=(),
        observed=ObservedTasks(
            tasks=[
                TaskObservation(
                    identifier=IDENT,
                    state=TaskState.COMPLETE,
                    task=None,
                    current=attempt("logs/b.eval"),
                )
            ]
        ),
    )

    assert read_runs(workspace.marks_runs) == {}


# --- the record ------------------------------------------------------------


def test_the_record_folds_and_the_process_table_says_which_are_live(
    tmp_path: Path,
) -> None:
    record = tmp_path / "runs.jsonl"
    target = Target(
        task="t", location="logs/a.eval", eval_id="e", sample_id="1", epoch=2, uuid="u"
    )
    for run, pid in (("a-1", 11), ("a-2", None), ("b-1", 12)):
        record_intent(
            record,
            run=run,
            class_key="a" if run.startswith("a") else "b",
            ruling_ts="T0",
            disposition=Disposition.ZERO,
            targets=[target],
            argv=["python", "-m", "inspect_steward", "_mark", "--run", run],
        )
        if pid is not None:
            record_launched(record, run=run, pid=pid)
    record_exited(record, run="b-1", status=1, detail="the claim never freed")

    runs = resolve_runs(record, scan=lambda: {11: "a-1", 12: "b-1", 13: "a-2"})

    # a pid carrying the id is live; a run whose spawn never returned is not,
    # whatever else carries its id; an exited run is finished however the
    # table reads
    assert runs.live == {"a-1"}
    assert runs.running("a", "T0") is not None
    assert [run.run for run in runs.finished("a", "T0")] == ["a-2"]
    assert runs.running("b", "T0") is None
    (failed,) = runs.finished("b", "T0")
    assert failed.exited and failed.status == 1 and failed.detail
    # and the target round-trips whole
    assert runs.by_run["a-1"].targets == (target,)
    assert runs.by_run["a-1"].argv[-1] == "a-1"


def test_a_line_this_version_cannot_read_costs_its_run_and_nothing_else(
    tmp_path: Path,
) -> None:
    record = tmp_path / "runs.jsonl"
    record.write_text(
        '{"ts": "2026-08-30T10:00:00Z", "type": "intent", "run": "x-1", '
        '"class": "a", "for": "T0", "disposition": "sideways", "targets": []}\n'
        '{"ts": "2026-08-30T10:00:01Z", "type": "launched", "run": "y-1", "pid": 3}\n',
        encoding="utf-8",
    )

    assert read_runs(record) == {}


def test_the_layout_keeps_a_run_out_of_the_workers_directory(tmp_path: Path) -> None:
    workspace = Workspace.at(tmp_path)

    assert workspace.marks == workspace.state / "marks"
    assert workspace.marks_workers == workspace.marks / "workers"
    assert workspace.marks_inflight == workspace.marks / "inflight.jsonl"
    assert workspace.marks_runs == workspace.marks / "runs.jsonl"
    assert workspace.marks_run("abc-1") == workspace.marks / "abc-1"
    assert not workspace.marks_workers.is_relative_to(workspace.workers)


def test_the_applied_fold_reads_what_a_run_wrote() -> None:
    fold = read_applied(
        [
            JournalEvent.model_validate(
                {
                    "ts": "2026-08-30T11:00:00Z",
                    "type": ACTION,
                    "action": RULING_APPLIED,
                    "class": CLASS,
                    "for": "T0",
                    "run": "abc-1",
                    "edited": [
                        {
                            "task": "t",
                            "location": "l",
                            "eval_id": "e",
                            "uuids": ["u1", "u2"],
                            "scores": ["exact"],
                        }
                    ],
                }
            )
        ]
    )

    assert fold.edited_uuids(CLASS, "T0") == {"u1", "u2"}
    assert fold.runs(CLASS, "T0") == {"abc-1"}
    assert fold.witness(CLASS, "T0", "t") == "2026-08-30T11:00:00Z"
    assert fold.edited_uuids(CLASS, "T1") == frozenset()
