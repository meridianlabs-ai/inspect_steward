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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

import psutil

from .._schedule import RunningWorker
from .._util.process import UNREADABLE
from .ctl import TaskRow, Unavailable, cancel_task, list_tasks
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

    LEFT = "left"
    """Still running, deliberately. Some of this process's tasks are wanted and the ones that are not could not be told apart from them, so nothing was stopped — see `_stop`."""


@dataclass(frozen=True)
class Stop:
    """One worker, and what stopping it came to."""

    worker: str
    """Worker stem."""

    identifiers: tuple[str, ...]
    """The tasks this was about. Every task the worker holds where the whole process was stopped; the subset that was cancelled where it was not."""

    outcome: Stopped

    detail: str = ""
    """Why it went this way, when the way it went was not the good one. Empty for a clean cancel."""

    @property
    def graceful(self) -> bool:
        """Whether the tasks are stopping and their results survived.

        `GONE` counts, because nothing was taken from a worker that had already finished. `LEFT` does not, and that is the whole distinction: it took nothing because it did nothing, so the tasks the caller asked to stop are still running and the caller has to say so. Reporting it as graceful would tell an operator their archived task was dealt with while it goes on writing into `logs/`.
        """
        return self.outcome in (Stopped.CANCELLED, Stopped.GONE)


@dataclass(frozen=True)
class StopRequest:
    """A worker, and which of its tasks are to be stopped."""

    worker: RunningWorker

    identifiers: tuple[str, ...]
    """The tasks to stop. Equal to the worker's own list where the whole process goes, and a subset where a packed worker is holding work that is still wanted."""

    @property
    def whole(self) -> bool:
        """Whether stopping this leaves the process nothing to do, and so may end it outright."""
        return set(self.identifiers) >= set(self.worker.identifiers)


def stop_workers(
    requests: Sequence[StopRequest], *, locations: Mapping[str, str] | None = None
) -> list[Stop]:
    """Stop the named tasks, preferring to ask over to signal.

    The task listing is read **once** for the whole set, which is the property that made `inspect ctl` the right client for mutations in the first place: it spans every live process in a single invocation, so stopping a fleet of ten costs one ~1.3s read rather than ten (`ctl.py`).

    **A process is only ended when nothing wanted is left in it.** At the default width a worker holds one task, so stopping that task and stopping the process are the same act and this is invisible. Once a run is packed they come apart: a task that leaves the manifest may be sharing a process with several that did not, and signalling it would destroy hours of work nobody asked to lose. So a partial stop cancels its tasks individually and lets the process carry on, and only a whole one may escalate to a signal.

    **No `reason` parameter, unlike a retune.** `ctl task cancel` takes none, and it needs none: a retune leaves a changed number that only a reason explains, where a cancel leaves a finalized log that says it was cancelled. Why it was cancelled belongs in the caller's own record, which is where the caller already writes it.

    Args:
        requests: What to stop, as `resolve_inflight` reported the workers running.
        locations: Log location to task identifier, from the caller's observation of the log directory. Only consulted for a partial stop, which is the one case that has to tell a process's tasks apart; the control channel names the log a task is writing, and this is what turns that into an identifier Steward knows.

    Returns:
        One entry per request, in the order given. Never raises: a worker that could not be stopped is a fact to report, and one failure must not leave the rest of the set running.
    """
    if not requests:
        return []

    listing = list_tasks([request.worker.pid for request in requests])
    rows: dict[int, list[TaskRow]] = {}
    if not isinstance(listing, Unavailable):
        for row in listing:
            rows.setdefault(row.pid, []).append(row)
    unreadable = listing.detail if isinstance(listing, Unavailable) else ""

    return [
        _stop(request, rows.get(request.worker.pid, []), unreadable, locations or {})
        for request in requests
    ]


def _stop(
    request: StopRequest,
    rows: list[TaskRow],
    unreadable: str,
    locations: Mapping[str, str],
) -> Stop:
    """Stop one worker's share, asking first and signalling only if there is nobody to ask."""
    worker = request.worker
    if not request.whole:
        # a packed worker holding work that is still wanted. Its tasks have to
        # be told apart, and the log each one is writing is what does it -- so
        # a row Steward cannot place is a row it must not cancel, because the
        # cost of guessing wrong is cancelling a task somebody still wants
        wanted = set(request.identifiers)
        matched = [
            row
            for row in rows
            if row.log_location and locations.get(row.log_location) in wanted
        ]
        if len(matched) != len(wanted):
            return Stop(
                worker=worker.worker,
                identifiers=request.identifiers,
                outcome=Stopped.LEFT,
                detail=unreadable
                or (
                    f"only {len(matched)} of {len(wanted)} tasks could be matched "
                    f"to what the control channel reported, and the rest of this "
                    f"process is still wanted"
                ),
            )
        return _cancel(request, matched)

    if not rows:
        # either the fleet listing failed outright, or it succeeded and this
        # process is not running a task -- pre-boundary, or already on its way
        # out. Neither has a task to cancel
        return _signal(
            worker,
            unreadable
            or "the control channel reported no task running in this process",
        )

    return _cancel(request, rows)


def _cancel(request: StopRequest, rows: list[TaskRow]) -> Stop:
    """Ask for every one of these tasks, escalating only where the whole process is going."""
    refused = [
        outcome.detail
        for row in rows
        if isinstance(outcome := cancel_task(row.task_id), Unavailable)
    ]
    if refused and request.whole:
        return _signal(
            request.worker, f"the cancel was not accepted ({'; '.join(refused)})"
        )
    return Stop(
        worker=request.worker.worker,
        identifiers=request.identifiers,
        # a refused cancel on a partial stop is not escalated: the process is
        # still running work somebody wants, and there is no signal that ends
        # one task of it
        outcome=Stopped.LEFT if refused else Stopped.CANCELLED,
        detail=f"the cancel was not accepted ({'; '.join(refused)})" if refused else "",
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
                identifiers=worker.identifiers,
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
                identifiers=worker.identifiers,
                outcome=outcome,
                detail=detail,
            )

    return Stop(
        worker=worker.worker,
        identifiers=worker.identifiers,
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
