"""Detection: one turn's full anomaly census, composed from what the turn already read.

This is the projection half of step 23 — the `absorb` step (`_anomaly.fold`) diffs what this module returns against the journal, so `detect()` recomputes **everything observable** every turn, idempotently, and never writes. A `status` runs it and journals nothing; losing the cache re-derives the same census.

Five signatures, each read at the cheapest tier that can answer it:

- `error:{type}@{frame}` and `limit:operator` — sample-level, from `classed_instances`' summaries and single-sample reads (`_evalset.instances`).
- `task:error[:{type}@{frame}]` — a log that finished `status="error"`, classed from the header's own error, no new read.
- `task:vanished` — a started log whose worker is gone. The log's contents do not discriminate an OOM from a lost host, so the class does not pretend to.
- `task:no-log[-exit:{type}@{frame}]` — a departed worker that left no log at all. The only evidence is its output tail, so the tail is what classes it (the exit-status sentry was considered and rejected: tail-only, no spawn changes).
- `score:zero:{name}:{digest}` — a COMPLETE task whose headline is exactly zero over enough samples, confirmed by the one summaries read its log already got. Narrow by design: headline `0.0`, at least `UNIFORM_ZERO_MIN` samples, and every score converting to zero — a cancellation artifact or a legitimately hard task does not trip it.

Orphaned tasks are skipped whole — their logs are leaving, not failing — and cancelled logs contribute no task signature, because cancellation is somebody's decision already.
"""

from dataclasses import dataclass, field
from pathlib import Path

from .._anomaly.fold import TaskHealth
from .._evalset.classify import (
    MESSAGE_CAP,
    VANISHED,
    cancelled,
    kind_of,
    no_log_class,
    parse_error,
    substrate,
    task_error_class,
    zero_class,
)
from .._evalset.instances import (
    ClassedCache,
    Instance,
    InstanceBatch,
    classed_instances,
)
from .._evalset.observe import (
    LogAttempt,
    ObservedLogs,
    ObservedTasks,
    TaskState,
    UnreadableLog,
)
from .._schedule.reconcile import InFlight
from .._worker.live import LiveFleet

UNIFORM_ZERO_MIN = 10
"""Samples a log must hold before a zero headline is worth confirming. Below this, one task of a handful of hard samples trips the detector more often than a broken grader does."""

TAIL_BYTES = 4096
"""How much of a departed worker's output file is read for its traceback. A definition that dies on import prints one standard traceback at the end; anything that needs more than this is investigation material either way."""


@dataclass(frozen=True)
class Detection:
    """The census: every instance of every class observed this turn."""

    batches: list[InstanceBatch] = field(default_factory=list[InstanceBatch])
    """One batch per class, sorted by class key."""

    unreadable: list[UnreadableLog] = field(default_factory=list[UnreadableLog])
    """Settled logs whose summaries would not read — real damage, merged into the existing unreadable item path by the turn."""


def detect(
    observed: ObservedTasks,
    logs: ObservedLogs,
    inflight: InFlight,
    fleet: LiveFleet,
    *,
    workers_dir: Path,
    cache: ClassedCache,
) -> Detection:
    """Compose the turn's anomaly census.

    Args:
        observed: The manifest read against the log directory — what says which identifiers are the run's own and which tasks stand complete.
        logs: The directory, as observation read it.
        inflight: The worker record — running workers gate `task:vanished`, departed ones are `task:no-log` candidates.
        fleet: The live fleet — its per-task error counts are the gate that keeps a healthy running log unread.
        workers_dir: `.steward/workers/`, where a departed worker's output tail lives.
        cache: What previous turns classified. Filled in as this turn reads; the caller prunes and writes it back on execute.

    Returns:
        One batch per class, and what could not be read.
    """
    tracked = _tracked(observed, logs)
    classed = classed_instances(
        tracked, errored_running=_errored_running(tracked, fleet), cache=cache
    )

    instances = list(classed.instances)
    instances.extend(_task_failures(tracked, inflight))
    instances.extend(_departed_without_logs(observed, logs, inflight, workers_dir))
    instances.extend(_uniform_zeros(observed, classed.zero))

    return Detection(batches=_batched(instances), unreadable=classed.unreadable)


def task_health(observed: ObservedTasks) -> dict[str, TaskHealth]:
    """Per task, whether it stands recovered — what resolution detection consumes.

    A task with an unreadable log is not recovered, whatever its headers say: the census is blind to whatever that log holds, and both pass branches trust the census's silence — the landed one that a new attempt carried no fresh failures, the warm one that nothing of the ruled population is left unapplied. A recovery the fold cannot verify waits for the read; the `unreadable` item is already saying why.
    """
    unreadable = {entry.location for entry in observed.unreadable}
    health: dict[str, TaskHealth] = {}
    for task in observed.tasks:
        if task.task is None:
            continue
        complete = task.state is TaskState.COMPLETE and not any(
            attempt.location in unreadable
            for attempt in (task.current, *task.superseded)
            if attempt is not None
        )
        health[task.identifier] = TaskHealth(
            complete=complete,
            settled=task.current.created
            if complete and task.current is not None
            else "",
        )
    return health


def _tracked(observed: ObservedTasks, logs: ObservedLogs) -> ObservedLogs:
    """The directory narrowed to the run's own identifiers.

    An orphan's logs are on their way to the archive; classifying their failures would open windows nothing can ever resolve.
    """
    orphaned = {
        task.identifier for task in observed.tasks if task.state is TaskState.ORPHANED
    }
    if not orphaned:
        return logs
    return ObservedLogs(
        log_dir=logs.log_dir,
        attempts={
            identifier: attempts
            for identifier, attempts in logs.attempts.items()
            if identifier not in orphaned
        },
        unreadable=logs.unreadable,
    )


def _errored_running(logs: ObservedLogs, fleet: LiveFleet) -> set[str]:
    """Running logs worth a summaries read: their own worker says errors exist."""
    gated: set[str] = set()
    for identifier, attempts in logs.attempts.items():
        live = fleet.tasks.get(identifier)
        if live is None or live.unavailable is not None or live.samples.errored <= 0:
            continue
        gated.update(
            attempt.location for attempt in attempts if attempt.status == "started"
        )
    return gated


def _task_failures(logs: ObservedLogs, inflight: InFlight) -> list[Instance]:
    """`task:error` for halted logs, `task:vanished` for abandoned ones."""
    running = inflight.running_identifiers
    instances: list[Instance] = []
    for identifier, attempts in logs.attempts.items():
        for attempt in attempts:
            if attempt.status == "error" and not cancelled(attempt.error):
                parsed = parse_error(attempt.error, attempt.error_traceback)
                instances.append(
                    _task_instance(
                        identifier,
                        attempt,
                        class_key=task_error_class(
                            attempt.error, attempt.error_traceback
                        ),
                        message=(attempt.error or "")[:MESSAGE_CAP],
                        flagged=substrate(parsed, attempt.error),
                    )
                )
            elif attempt.status == "started" and identifier not in running:
                instances.append(
                    _task_instance(
                        identifier,
                        attempt,
                        class_key=VANISHED,
                        message="the log is mid-write and its worker is gone",
                        flagged=False,
                    )
                )
    return instances


def _task_instance(
    identifier: str,
    attempt: LogAttempt,
    *,
    class_key: str,
    message: str,
    flagged: bool,
) -> Instance:
    return Instance(
        class_key=class_key,
        ref=f"{identifier}@{attempt.eval_id}",
        task=identifier,
        location=attempt.location,
        message=message,
        attempt_created=attempt.created,
        substrate=flagged,
        eval_id=attempt.eval_id,
    )


def _departed_without_logs(
    observed: ObservedTasks,
    logs: ObservedLogs,
    inflight: InFlight,
    workers_dir: Path,
) -> list[Instance]:
    """`task:no-log` — a worker died and its task has no log at all to say why.

    The bar is deliberately *any* attempt, not an attempt newer than the spawn: a resumed worker continues a log whose `created` predates it, so a newer-than test would report the same death twice — once as `vanished` off the log, once here off the tail. Where a log exists, whatever its age, the log is the evidence and the error/vanished signatures carry it; only a task with nothing in the directory has the worker's output tail as its whole story. Orphans never reach here — a departed worker's identifiers come from the manifest that launched it.
    """
    orphaned = {
        task.identifier for task in observed.tasks if task.state is TaskState.ORPHANED
    }
    instances: list[Instance] = []
    for worker in inflight.departed:
        tail: str | None = None
        flagged = False
        for identifier in worker.identifiers:
            if identifier in orphaned or logs.attempts.get(identifier):
                continue
            if tail is None:
                tail = _tail(workers_dir / f"{worker.worker}.log")
                # the whole tail as the message, deliberately: an ENOSPC or a
                # credentials failure printed anywhere in a dying worker's last
                # 4KB is the substrate whatever raised it
                flagged = substrate(parse_error(None, tail), tail)
            instances.append(
                Instance(
                    class_key=no_log_class(tail),
                    ref=f"{identifier}@{worker.worker}",
                    task=identifier,
                    message=tail[-MESSAGE_CAP:] if tail else "no output at all",
                    attempt_created=worker.started,
                    substrate=flagged,
                )
            )
    return instances


def _tail(output: Path) -> str:
    """The end of a worker's output file, or empty for one that wrote nothing."""
    try:
        with output.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - TAIL_BYTES))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _uniform_zeros(observed: ObservedTasks, zero: dict[str, bool]) -> list[Instance]:
    """`score:zero` — a completed task whose every score converts to zero."""
    instances: list[Instance] = []
    for task in observed.tasks:
        attempt = task.current
        if (
            task.task is None
            or task.state is not TaskState.COMPLETE
            or attempt is None
            or attempt.headline != 0.0
            or attempt.total_samples < UNIFORM_ZERO_MIN
            or not zero.get(attempt.location, False)
        ):
            continue
        instances.append(
            Instance(
                class_key=zero_class(task.key, task.identifier),
                ref=f"{task.identifier}@{attempt.eval_id}",
                task=task.identifier,
                location=attempt.location,
                message=(
                    f"headline {attempt.headline_name or 'metric'} is 0.0 and every "
                    f"score in {attempt.total_samples} samples converts to zero"
                ),
                attempt_created=attempt.created,
                eval_id=attempt.eval_id,
            )
        )
    return instances


def _batched(instances: list[Instance]) -> list[InstanceBatch]:
    grouped: dict[str, list[Instance]] = {}
    for instance in instances:
        grouped.setdefault(instance.class_key, []).append(instance)
    return [
        InstanceBatch(
            class_key=class_key,
            kind=kind_of(class_key),
            # eager by design: one substrate-flagged instance colours the class
            substrate=any(instance.substrate for instance in listed),
            instances=tuple(listed),
        )
        for class_key, listed in sorted(grouped.items())
    ]


__all__ = [
    "TAIL_BYTES",
    "UNIFORM_ZERO_MIN",
    "Detection",
    "detect",
    "task_health",
]
