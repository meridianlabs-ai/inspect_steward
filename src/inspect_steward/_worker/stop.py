"""Stopping a worker that is running something nobody wants any more.

The first thing in Steward that ends work rather than starting it, and the reason it exists is the archive gate: `launch` may commit a manifest that no longer names a task some worker is still running, and leaving that worker alone would have it write into `logs/` for hours against a definition nothing agrees with. Later verbs need the same primitive — `steward stop`, and the smoke's wall-clock cap, which is a deadline Steward enforces by stopping workers because `time_limit` cannot be passed through (workflow.md §7.1).

**Cancelling is not killing, and the whole design turns on the difference.** `inspect ctl task cancel` keeps every completed sample, finalizes the log, and lets the process exit on its own — so the partial result *lands*, and what happens to it afterwards is an archive rather than a deletion. That is the only way to stop a worker that honours *Steward never destroys a result*. A signal loses whatever has not been flushed, which for a task twelve hours into a five-hundred-sample dataset is the expensive half of a night.

**So this does not wait for the exit, and that is the point rather than a shortcut.** A cancelled worker finalizes its log and goes, taking as long as that takes — seconds locally, considerably longer flushing to S3. Waiting for it would put an unbounded duration inside a claim, which is exactly the invariant that keeps a claim short-lived and therefore keeps *a claim older than the threshold is wedged* true (execution.md, *A scan is a detached process, not part of a tend*). The exit is observed the way every other exit is: the next turn does not find the process and reaps it. A worker that was told to stop and has not finished stopping is in flight, which is a state the loop already knows how to hold.

**Signals are for a worker that cannot be told anything**, and only then. No control socket yet (the pre-boundary window, where there is no log to lose), or a control channel that will not answer — in both cases nothing has *asked* the worker to stop, so the alternative to a signal is a process running an orphaned task until somebody notices it in `ps`. `SIGTERM` then `SIGKILL`, reusing the escalation `_workspace.claim` already established rather than inventing a second shape for it.

**A cancelled worker's log lands as `status="error"`, and the next caller of this needs to know it.** Measured rather than assumed (`tests/launch/test_stop_live.py`): inspect finalizes the log with `TerminateTaskError('Task cancelled by user (abort)')` and the in-flight sample carries a `CancelledError`, so `observe` reads `IncompleteReason.ERROR` and `reconcile` treats the task as **incomplete** — which for a task still in the manifest means the next tend spawns it again, bounded only by the stall guard.

That is harmless for the one caller there is today, because a launch only stops workers whose task has *left* the manifest: an orphan is archived rather than respawned. It is not harmless for the two callers coming. `steward stop` and the smoke's wall-clock cap both stop a worker whose task desired state still names, so each will need something that says *do not start this again* — a pause, or a decision recorded against the identifier. Neither is this module's to invent, and inventing one here would be a policy nothing yet reads.

**The pid is re-confirmed immediately before the signal**, for the reason `claim._signalable` spells out at greater length: a pid is only a name for a process while that process is alive, and a worker that exited between the resolve and the kill leaves its number free for somebody else. The confirmation is one read of the target's environment, which a worker fills with its own identity precisely so that questions like this one have an answer (`inflight.py`).
"""

import os
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import psutil

from .._schedule import RunningWorker
from .._util.process import UNREADABLE
from .ctl import Unavailable, cancel_task, list_tasks
from .inflight import STEWARD_WORKER

TERM_GRACE = 5.0
"""Seconds to wait for a signalled worker to go before escalating. The same figure `_workspace.claim` uses, and for the same reason: it is a wait on a process handling a signal, not on one flushing a log — that path never gets here."""

KILL_GRACE = 2.0
"""Seconds to wait for the kernel after `SIGKILL`, which the target has no say in."""

_POLL = 0.05


class Stopped(StrEnum):
    """How a worker was stopped, which is also how much of its work survived."""

    CANCELLED = "cancelled"
    """Asked through its control channel. Completed samples are kept and the log is finalized; the process is still exiting, and a later turn reaps it."""

    TERMINATED = "terminated"
    """Signalled, and gone. Whatever it had not flushed is lost — the case where nothing could be asked."""

    KILLED = "killed"
    """Signalled, ignored it, and killed."""

    GONE = "gone"
    """Already not there. The ordinary race, not a failure: workers run for hours and this runs for milliseconds, so any of them may finish in between."""

    SURVIVED = "survived"
    """Still running after everything. Reported so the caller can say so — nothing here retries, because a process that outlived `SIGKILL` is wedged in the kernel and no amount of asking again changes that."""


@dataclass(frozen=True)
class Stop:
    """One worker, and what stopping it came to."""

    worker: str
    """Worker stem."""

    identifier: str
    outcome: Stopped

    detail: str = ""
    """Why it went this way, when the way it went was not the good one. Empty for a clean cancel."""

    @property
    def graceful(self) -> bool:
        """Whether this worker's results survived. `GONE` counts: nothing was taken from it."""
        return self.outcome in (Stopped.CANCELLED, Stopped.GONE)


def stop_workers(workers: Sequence[RunningWorker]) -> list[Stop]:
    """Stop every one of these workers, preferring to ask over to signal.

    The task listing is read **once** for the whole set, which is the property that made `inspect ctl` the right client for mutations in the first place: it spans every live process in a single invocation, so stopping a fleet of ten costs one ~1.3s read rather than ten (`ctl.py`).

    **No `reason` parameter, unlike a retune.** `ctl task cancel` takes none, and it needs none: a retune leaves a changed number that only a reason explains, where a cancel leaves a finalized log that says it was cancelled. Why it was cancelled belongs in the caller's own record, which is where the caller already writes it.

    Args:
        workers: The workers to stop, as `resolve_inflight` reported them running.

    Returns:
        One entry per worker, in the order given. Never raises: a worker that could not be stopped is a fact to report, and one failure must not leave the rest of the set running.
    """
    if not workers:
        return []

    listing = list_tasks([worker.pid for worker in workers])
    tasks: dict[int, str] = (
        {}
        if isinstance(listing, Unavailable)
        else {row.pid: row.task_id for row in listing}
    )
    unreadable = listing.detail if isinstance(listing, Unavailable) else ""

    return [_stop(worker, tasks.get(worker.pid), unreadable) for worker in workers]


def _stop(worker: RunningWorker, task_id: str | None, unreadable: str) -> Stop:
    """Stop one worker, asking first and signalling only if there is nobody to ask."""
    if task_id is None:
        # either the fleet listing failed outright, or it succeeded and this
        # process is not running a task -- pre-boundary, or already on its way
        # out. Neither has a task to cancel
        return _signal(
            worker,
            unreadable
            or "the control channel reported no task running in this process",
        )

    outcome = cancel_task(task_id)
    if isinstance(outcome, Unavailable):
        return _signal(worker, f"the cancel was not accepted ({outcome.detail})")
    return Stop(
        worker=worker.worker,
        identifier=worker.identifier,
        outcome=Stopped.CANCELLED,
    )


def _signal(worker: RunningWorker, detail: str) -> Stop:
    """Escalate a worker out of existence, confirming its pid before each signal."""
    for sig, grace, outcome in (
        (signal.SIGTERM, TERM_GRACE, Stopped.TERMINATED),
        (signal.SIGKILL, KILL_GRACE, Stopped.KILLED),
    ):
        if not _is_worker(worker):
            return Stop(
                worker=worker.worker,
                identifier=worker.identifier,
                outcome=Stopped.GONE,
                detail=detail,
            )
        try:
            os.kill(worker.pid, sig)
        except OSError as ex:
            # gone between the confirmation and the signal, or not ours to
            # signal. The wait below is what decides which
            detail = f"{detail}; could not signal pid {worker.pid}: {ex}"
        if _departed(worker, grace):
            return Stop(
                worker=worker.worker,
                identifier=worker.identifier,
                outcome=outcome,
                detail=detail,
            )

    return Stop(
        worker=worker.worker,
        identifier=worker.identifier,
        outcome=Stopped.SURVIVED,
        detail=f"{detail}; pid {worker.pid} outlived SIGKILL",
    )


def _is_worker(worker: RunningWorker) -> bool:
    """Whether that pid is still the worker it was, and so still safe to signal.

    One read of one process rather than a table sweep: the worker put its own stem in its environment, so the question *is this still you* has a direct answer. A recycled pid fails it, and so does a pid that has become somebody else's process entirely — which is the whole hazard, since the alternative is signalling a stranger.
    """
    try:
        return psutil.Process(worker.pid).environ().get(STEWARD_WORKER) == worker.worker
    except UNREADABLE:
        # unreadable is *not one of ours* everywhere else in this codebase, and
        # here that reading is the safe one too: it declines to signal
        return False


def _departed(worker: RunningWorker, grace: float) -> bool:
    """Poll until the worker is no longer there, or until a deadline."""
    deadline = time.monotonic() + grace
    while True:
        if not _is_worker(worker):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


__all__ = [
    "KILL_GRACE",
    "TERM_GRACE",
    "Stop",
    "Stopped",
    "stop_workers",
]
