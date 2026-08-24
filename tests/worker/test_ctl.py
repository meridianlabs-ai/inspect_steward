"""Talking to a live worker, and to one that has already gone.

The second is the ordinary case, so most of this is about it. Three of the
outcomes can be produced for real and cheaply — an empty fleet, a task id that
matches nothing, a command the CLI rejects — and those get real invocations at
~1.3s each. The rest cannot be manufactured on demand (no eval is reliably
wedged, no CLI reliably emits a malformed body), so they go through `_decode`,
which exists split out for exactly that reason.

One launch carries every claim that needs a running worker.
"""

import time
from pathlib import Path

import pytest
from inspect_ai._eval.evalset import task_identifier
from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_steward import read_eval_set
from inspect_steward._worker import (
    ConfigView,
    TaskRow,
    Unavailable,
    list_tasks,
    task_config,
)
from inspect_steward._worker.ctl import ABSENT, _ctl, _decode
from inspect_steward._worker.spawn import SpawnedWorker

from ._fleet import FIXTURES, action, fleet, output

# --- classification -----------------------------------------------------

CASES = [
    ("null is nothing to target", 0, "null", "", ABSENT),
    (
        "an error envelope carries its kind",
        1,
        '{"error": {"kind": "not_found", "message": "no"}}',
        "",
        "not_found",
    ),
    (
        "a kind this version has not heard of survives",
        1,
        '{"error": {"kind": "wedged", "message": "?"}}',
        "",
        "wedged",
    ),
    (
        "an error with no kind is internal",
        1,
        '{"error": {"message": "?"}}',
        "",
        "internal",
    ),
    (
        "an unparseable error is internal",
        1,
        '{"error": "just a string"}',
        "",
        "internal",
    ),
    ("a malformed body is a broken contract", 0, "{not json", "", "invalid_response"),
    ("an array is a broken contract", 0, "[1, 2]", "", "invalid_response"),
    ("no output at all is internal", 1, "", "something went wrong", "internal"),
]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "kind"),
    [pytest.param(*case[1:], id=case[0].replace(" ", "_")) for case in CASES],
)
def test_an_outcome_maps_to_one_kind(
    returncode: int, stdout: str, stderr: str, kind: str
) -> None:
    result = _decode(returncode, stdout, stderr, command="task --json")
    assert isinstance(result, Unavailable)
    assert result.kind == kind
    assert result.detail


def test_a_document_decodes_to_itself() -> None:
    result = _decode(0, '{"as_of": 1.0, "tasks": []}', "", command="task --json")
    assert result == {"as_of": 1.0, "tasks": []}


def test_a_usage_error_is_ours_and_raises() -> None:
    # exit 2 means Steward built a command line the CLI does not accept, which
    # no caller can act on and none should have to check for
    with pytest.raises(RuntimeError, match="usage error"):
        _decode(2, "", "no such option: --nope", command="task --nope")


# --- against the real CLI, with nothing running -------------------------


def test_an_empty_fleet_is_an_answer_not_a_failure() -> None:
    # the assumption most likely to drift, and the one every tend rests on:
    # "no workers" has to be an empty list rather than an error
    assert list_tasks({1, 2, 3}) == []


def test_a_task_that_matches_nothing_is_absent() -> None:
    result = task_config("no-such-task")
    assert isinstance(result, Unavailable)
    # `null` on exit 0 -- which is why the exit code alone is not enough
    assert result.kind == ABSENT


def test_a_command_that_never_returns_is_busy_rather_than_gone() -> None:
    # an invocation takes ~1.3s, so this always expires -- and the point is the
    # classification: a client that timed out has learned nothing about whether
    # the worker is alive, so it must not report it dead
    result = _ctl("task", "--json", timeout=0.01)
    assert isinstance(result, Unavailable)
    assert result.kind == "busy"


def test_a_change_without_a_reason_is_refused_before_it_runs() -> None:
    # the reason is what annotates the record inspect writes into the eval log;
    # a retune nobody can review later is the thing worth preventing
    with pytest.raises(ValueError, match="reason"):
        task_config("any-task", max_samples=8)


# --- one live worker ----------------------------------------------------


def first_row(worker: SpawnedWorker, timeout: float = 120) -> TaskRow:
    """Wait for a worker to register its task, and check the filter on the way.

    Polling and filtering in one call rather than two, because an invocation
    costs ~1.3s and the eval it is asking about has to outlast every one of
    them.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = list_tasks({worker.pid, worker.pid + 100_000})
        assert not isinstance(found, Unavailable), found
        if found:
            # the bogus pid never appears: the listing spans every Inspect
            # process on the machine, and only this worker is ours
            assert [row.pid for row in found] == [worker.pid]
            return found[0]
        assert time.monotonic() < deadline, "the worker never registered a task"
        time.sleep(0.1)


def test_the_live_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read a running worker, retune it, and then watch every call fail well.

    One launch, because a worker costs seconds and these claims are stages of
    one lifecycle rather than independent facts.
    """
    gate = tmp_path / "gate"
    monkeypatch.setenv("STEWARD_TEST_GATE", str(gate))
    # long enough to outlast every invocation below, and killed when done
    monkeypatch.setenv("STEWARD_TEST_SLEEP", "120")
    workers = fleet(FIXTURES / "gated_evalset.py", tmp_path)
    manifest = read_eval_set(workers.definition, cwd=tmp_path)
    task = manifest.tasks[0]
    worker = workers.spawn(action(task.identifier, key=task.key))

    try:
        gate.touch()
        row = first_row(worker)

        # the join that makes the channel usable at all: a row Steward can
        # attribute to the worker it spawned, naming the log it will land
        assert row.status == "running"
        assert row.log_location is not None
        assert row.samples.total == 1

        view = task_config(row.task_id)
        assert not isinstance(view, Unavailable)
        assert view.max_samples is not None
        assert not view.applied

        # a dry run reports without moving anything
        rehearsal = task_config(row.task_id, max_samples=3, reason="test", dry_run=True)
        assert not isinstance(rehearsal, Unavailable)
        assert rehearsal.dry_run and not rehearsal.applied
        assert rehearsal.requested == {"max_samples": 3}
        assert reread(row.task_id).max_samples == view.max_samples

        applied = task_config(row.task_id, max_samples=3, reason="test")
        assert not isinstance(applied, Unavailable)
        assert applied.applied
        assert applied.max_samples == 3
        # the change is in the eval log, which is what makes it reviewable --
        # reported per knob, because applying and recording can differ
        assert applied.persisted == {"max_samples": True}

        # the setpoint is read back rather than remembered: Steward re-derives
        # worker state each tend instead of trusting what it last set
        assert reread(row.task_id).max_samples == 3
    finally:
        kill(worker)

    # and once the worker is gone, every call says so rather than raising
    assert list_tasks({worker.pid}) == []
    after = task_config(row.task_id)
    assert isinstance(after, Unavailable)
    assert after.kind == ABSENT


def test_a_patched_token_limit_does_not_break_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hazard exec §8.5 declined to guess at.

    `token_limit` is retunable *and* is an input to `task_identifier`, so a
    patched value written back into the log's `eval.config` would leave that
    log correlating to no manifest entry — a finished task reading as an
    orphan. The help text says these are live overrides read where limits are
    checked; this is the check.
    """
    gate = tmp_path / "gate"
    monkeypatch.setenv("STEWARD_TEST_GATE", str(gate))
    workers = fleet(FIXTURES / "gated_evalset.py", tmp_path)
    manifest = read_eval_set(workers.definition, cwd=tmp_path)
    task = manifest.tasks[0]
    worker = workers.spawn(action(task.identifier, key=task.key))

    try:
        gate.touch()
        # through `_ctl` rather than `task_config`, which deliberately exposes
        # `max_samples` only -- this knob is what is being investigated, not
        # something Steward offers
        patch = _ctl(
            "config",
            first_row(worker).task_id,
            "--token-limit",
            "12345",
            "--reason",
            "test",
            "--json",
        )
        assert not isinstance(patch, Unavailable), patch
        assert worker.process.wait(timeout=300) == 0, output(worker)
    finally:
        kill(worker)

    landed = list_eval_logs(str(tmp_path / "logs"))
    assert len(landed) == 1
    log = read_eval_log(landed[0], header_only=True)
    assert task_identifier(log, None) == task.identifier


def reread(task_id: str) -> ConfigView:
    view = task_config(task_id)
    assert not isinstance(view, Unavailable)
    return view


def kill(worker: SpawnedWorker) -> None:
    if worker.process.poll() is None:
        worker.process.kill()
    worker.process.wait(timeout=60)
