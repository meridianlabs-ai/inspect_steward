"""Stopping *some* of a process's tasks, which only a packed run ever asks for.

The whole-process paths are exercised end to end against a real worker in
`tests/launch/test_stop_live.py`, where the claim that matters — a cancelled
worker's finished samples actually land — is a property of inspect's cancel path
rather than of this module. What that test cannot reach is the partial stop: it
needs one process holding a task that is leaving and a task that is staying, and
it needs the correlation between them to *fail*, which a real worker will not do
on request.

So the fake is at one seam and one only, `inspect ctl` — a subprocess that
neither depends on nor observes anything here. Everything above it is real.
"""

from typing import Any

import pytest
from inspect_steward._schedule import RunningWorker
from inspect_steward._worker import Stopped, StopRequest, stop_workers
from inspect_steward._worker.ctl import TaskRow, Unavailable

WORKER = RunningWorker(
    worker="pair_abc12345_1",
    identifiers=("leaving", "staying"),
    pid=4242,
    host="host",
)

LOCATIONS = {"logs/leaving.eval": "leaving", "logs/staying.eval": "staying"}


def rows(*identifiers: str) -> list[TaskRow]:
    """What the fleet listing reports for the worker, one row per named task."""
    return [
        TaskRow(
            pid=WORKER.pid,
            task_id=f"id-{identifier}",
            task=identifier,
            status="running",
            log_location=f"logs/{identifier}.eval",
        )
        for identifier in identifiers
    ]


@pytest.fixture
def cancelled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Task ids the run asked to cancel, in the order it asked."""
    asked: list[str] = []

    def cancel(task_id: str) -> dict[str, Any]:
        asked.append(task_id)
        return {}

    monkeypatch.setattr("inspect_steward._worker.stop.cancel_task", cancel)
    return asked


def fake_listing(
    monkeypatch: pytest.MonkeyPatch, listing: list[TaskRow] | Unavailable
) -> None:
    def list_tasks(pids: list[int]) -> list[TaskRow] | Unavailable:
        return listing

    monkeypatch.setattr("inspect_steward._worker.stop.list_tasks", list_tasks)


def test_a_partial_stop_cancels_only_its_own_task(
    monkeypatch: pytest.MonkeyPatch, cancelled: list[str]
) -> None:
    """The point of the whole partial path: the sibling is not touched."""
    fake_listing(monkeypatch, rows("leaving", "staying"))

    (stop,) = stop_workers(
        [StopRequest(worker=WORKER, identifiers=("leaving",))], locations=LOCATIONS
    )

    assert cancelled == ["id-leaving"]
    assert stop.outcome is Stopped.CANCELLED
    assert stop.graceful is True
    assert stop.identifiers == ("leaving",)


@pytest.mark.parametrize(
    "listing,expected",
    [
        pytest.param(rows("staying"), "matched", id="no_row_for_the_leaving_task"),
        pytest.param(
            [TaskRow(pid=WORKER.pid, task_id="id-x", task="x", status="running")],
            "matched",
            id="a_row_that_names_no_log_yet",
        ),
        pytest.param(
            Unavailable(kind="read_timeout", detail="ctl timed out"),
            "ctl timed out",
            id="no_ctl",
        ),
    ],
)
def test_an_uncorrelated_partial_stop_leaves_the_process_alone_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
    cancelled: list[str],
    listing: list[TaskRow] | Unavailable,
    expected: str,
) -> None:
    """`LEFT` is not graceful, because the task the caller asked to stop is still running.

    The pre-boundary window is the likely one: a task whose log has not appeared
    cannot be told from its siblings, and cancelling a guess would destroy work
    nobody asked to lose. So nothing is cancelled — and the caller has to hear
    that, or it reports an archived task as dealt with while it goes on writing
    into `logs/`.
    """
    fake_listing(monkeypatch, listing)

    (stop,) = stop_workers(
        [StopRequest(worker=WORKER, identifiers=("leaving",))], locations=LOCATIONS
    )

    assert cancelled == []
    assert stop.outcome is Stopped.LEFT
    assert stop.graceful is False
    assert expected in stop.detail


def test_a_refused_cancel_on_a_partial_stop_is_not_escalated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no signal that ends one task of a process, so a refusal is reported rather than acted on."""
    fake_listing(monkeypatch, rows("leaving", "staying"))

    def refuse(task_id: str) -> Unavailable:
        return Unavailable(kind="busy", detail="the worker refused")

    monkeypatch.setattr("inspect_steward._worker.stop.cancel_task", refuse)
    signalled: list[int] = []

    def kill(pid: int, sig: int) -> None:
        signalled.append(pid)

    monkeypatch.setattr("inspect_steward._worker.stop.os.kill", kill)

    (stop,) = stop_workers(
        [StopRequest(worker=WORKER, identifiers=("leaving",))], locations=LOCATIONS
    )

    assert signalled == []
    assert stop.outcome is Stopped.LEFT
    assert stop.graceful is False


def test_a_whole_stop_of_a_packed_worker_needs_no_correlation(
    monkeypatch: pytest.MonkeyPatch, cancelled: list[str]
) -> None:
    """Every task is going, so there is nothing to tell apart and no `locations` to consult.

    Which is also why the default width never pays for correlation: one task is always the whole process.
    """
    fake_listing(monkeypatch, rows("leaving", "staying"))

    (stop,) = stop_workers([StopRequest(worker=WORKER, identifiers=WORKER.identifiers)])

    assert sorted(cancelled) == ["id-leaving", "id-staying"]
    assert stop.outcome is Stopped.CANCELLED
    assert stop.graceful is True


def test_nothing_to_stop_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty set costs no `inspect ctl` invocation, which is the expensive part."""

    def unreachable(pids: list[int]) -> list[TaskRow]:
        raise AssertionError("the fleet should not have been listed")

    monkeypatch.setattr("inspect_steward._worker.stop.list_tasks", unreachable)

    assert stop_workers([]) == []


def test_a_worker_with_no_task_running_is_signalled_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-boundary or already on its way out: there is nobody to ask, and the whole process is going anyway."""
    fake_listing(monkeypatch, [])

    def departed(worker: RunningWorker) -> bool:
        return False

    monkeypatch.setattr("inspect_steward._worker.stop._is_worker", departed)

    (stop,) = stop_workers([StopRequest(worker=WORKER, identifiers=WORKER.identifiers)])

    assert stop.outcome is Stopped.GONE
    assert stop.graceful is True
