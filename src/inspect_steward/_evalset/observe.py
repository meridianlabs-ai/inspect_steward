"""Reading a log directory as structured state.

Steward converges a log directory toward a manifest. This module is the read half of that: a directory of eval logs becomes an observation, and an observation plus a manifest becomes a per-task verdict — complete, incomplete, missing, or orphaned. `reconcile` turns those verdicts into actions; nothing here decides anything.

The work splits at the filesystem boundary rather than at the manifest:

- `observe_logs` does the I/O. It groups attempts by `task_identifier` and knows nothing about what was *supposed* to run — which is what lets it serve `logs-archive/` and any other directory where there is no manifest to compare against (workflow.md, *Steward never destroys a result, but it does curate the directory*).
- `observe_tasks` is pure. It answers completeness, which needs the manifest's per-task sample and epoch counts, and it names identifiers present in the directory but absent from the definition.

Two properties are borrowed from the journal, because they are properties of anything Steward reads on a schedule and cannot afford to choke on:

- **An unreadable log costs one log, never the directory.** A zip that exists but has not yet been written far enough to have a header is an ordinary transient during worker startup, and a tend that raised on it would be a tend that never ran. Damage is reported with a reason, and the caller decides whether to complain.
- **Only headers are ever read.** Never a sample, never a summary (agent.md, *Never read a full eval log*).
"""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Sequence

# task_identifier is the pairing mechanism eval_set() itself uses; it is
# versioned rather than public, which is why the manifest records its version
from inspect_ai._eval.evalset import task_identifier
from inspect_ai.log import (
    EvalLog,
    EvalLogInfo,
    list_eval_logs_async,
    read_eval_log_async,
)

from .manifest import Manifest, ManifestTask

DEFAULT_READ_CONCURRENCY = 20
"""Headers read at once. A ceiling against file descriptors and connection pools rather than a tuned number."""


@dataclass(frozen=True)
class LogAttempt:
    """One log file in a log directory.

    A task can have more than one — a failed attempt followed by a resumed one — and Steward keeps them all, because attempt history is the diagnostic material it exists to reason about (execution.md, *Multiple logs per task*).
    """

    location: str
    """Path the log was read from."""

    identifier: str
    """`task_identifier` computed from the header."""

    created: str
    """`eval.created`. The ordering key, and the only timestamp that survives the mid-run header fallback."""

    status: str
    """`started`, `success`, `cancelled`, or `error`."""

    invalidated: bool
    """Whether any samples in the log were invalidated."""

    error: str | None
    """Error that halted the eval, if any."""

    total_samples: int
    """Samples the log claims (0 when it has no results yet)."""

    completed_samples: int
    """Samples that finished without error."""

    epochs: int | None
    """Epochs the log ran with, as recorded in its config."""

    task: str
    """Task name, for display."""

    task_id: str
    """The log's own task id. Unpredictable to Steward — each worker resolves its own — which is why it is carried rather than computed (execution.md, *`eval-set.json` must be written incrementally*)."""

    eval_id: str
    mtime: float | None

    @property
    def errored_samples(self) -> int:
        """Samples that ran and failed.

        Nonzero is normal rather than exceptional: worker mode forces `fail_on_error=False`, so a task finishes `success` carrying whatever residue of errored samples it accumulated (execution.md, *Recovery*).
        """
        return max(0, self.total_samples - self.completed_samples)


@dataclass(frozen=True)
class UnreadableLog:
    """A file that looked like a log and could not be read as one."""

    location: str
    reason: str


@dataclass(frozen=True)
class ObservedLogs:
    """What a log directory contains, grouped by task identifier."""

    log_dir: str

    attempts: dict[str, list[LogAttempt]] = field(
        default_factory=dict[str, list[LogAttempt]]
    )
    """Attempts per identifier, newest first by `created`."""

    unreadable: list[UnreadableLog] = field(default_factory=list[UnreadableLog])

    @property
    def intact(self) -> bool:
        return not self.unreadable

    @property
    def count(self) -> int:
        """Total logs read."""
        return sum(len(attempts) for attempts in self.attempts.values())

    def current(self, identifier: str) -> LogAttempt | None:
        """The attempt that counts for an identifier.

        The latest *successful* attempt wins, falling back to the newest attempt when none succeeded. Deliberately not upstream's rule, which takes the newest by file mtime whatever its status: mtime is rewritten by restoring a log from the archive, and a re-run that errored should not displace a good result (execution.md, *Multiple logs per task*).
        """
        attempts = self.attempts.get(identifier)
        if not attempts:
            return None
        return next(
            (attempt for attempt in attempts if attempt.status == "success"),
            attempts[0],
        )

    def superseded(self, identifier: str) -> list[LogAttempt]:
        """Every attempt for an identifier other than the current one."""
        current = self.current(identifier)
        return [
            attempt
            for attempt in self.attempts.get(identifier, [])
            if attempt is not current
        ]


class TaskState(StrEnum):
    """What a task's logs say about it.

    Deliberately the domain of `reconcile`'s actions rather than a taxonomy of log conditions: each value maps to one thing to do.
    """

    COMPLETE = "complete"
    """Enough has been done. Leave it alone."""

    INCOMPLETE = "incomplete"
    """A log exists and more work is needed. Resume it — subject to nothing already running, which the in-flight record answers."""

    MISSING = "missing"
    """No log at all. Spawn it fresh."""

    ORPHANED = "orphaned"
    """A log whose identifier is not in the current definition. Archive it."""


class IncompleteReason(StrEnum):
    """Why an incomplete task is incomplete.

    Reporting material, not an input to the decision: every incomplete task takes the same action.
    """

    STARTED = "started"
    """The log never finished. Whether its worker is still alive is the in-flight record's to say."""

    SHORT = "short"
    """Succeeded, but with fewer samples than the manifest calls for — raised epochs, a widened limit, or holes."""

    INVALIDATED = "invalidated"
    """Someone invalidated samples in it."""

    ERROR = "error"
    CANCELLED = "cancelled"

    NO_RESULTS = "no_results"
    """Claims success but carries no results at all."""


@dataclass(frozen=True)
class TaskObservation:
    """One identifier's verdict."""

    identifier: str

    state: TaskState

    task: ManifestTask | None
    """The manifest row, or `None` for an orphan."""

    reason: IncompleteReason | None = None

    current: LogAttempt | None = None
    superseded: list[LogAttempt] = field(default_factory=list[LogAttempt])

    required_samples: int | None = None
    """Samples × epochs the manifest calls for (`None` for an orphan)."""

    @property
    def key(self) -> str:
        """Human-facing display key, falling back to the log's task name for an orphan."""
        if self.task is not None:
            return self.task.key
        return self.current.task if self.current is not None else self.identifier

    @property
    def observed_samples(self) -> int:
        return self.current.total_samples if self.current is not None else 0

    @property
    def errored_samples(self) -> int:
        return self.current.errored_samples if self.current is not None else 0


@dataclass(frozen=True)
class ObservedTasks:
    """A manifest read against a log directory."""

    tasks: list[TaskObservation]
    """One per manifest task, in manifest order, followed by orphans."""

    unreadable: list[UnreadableLog] = field(default_factory=list[UnreadableLog])

    def by_state(self, state: TaskState) -> list[TaskObservation]:
        return [task for task in self.tasks if task.state == state]


async def observe_logs_async(
    log_dir: str | Path,
    *,
    concurrency: int = DEFAULT_READ_CONCURRENCY,
) -> ObservedLogs:
    """Read a log directory into attempts grouped by task identifier.

    Reads headers only, and reads them concurrently — which matters for the `.eval` format Inspect writes by default, where a header read genuinely awaits on I/O, and matters most when `log_dir` is remote.

    Args:
        log_dir: Directory to read. A directory that does not exist is an empty observation.
        concurrency: Headers to read at once.

    Returns:
        Attempts per identifier and one entry per file that could not be read.
    """
    log_dir = str(log_dir)

    # not recursive: a log directory is flat by design, and scan output lives
    # in a subdirectory of it (execution.md, *One flat directory*)
    infos = await list_eval_logs_async(log_dir, recursive=False)

    semaphore = asyncio.Semaphore(concurrency)

    async def read(info: EvalLogInfo) -> LogAttempt | UnreadableLog:
        async with semaphore:
            try:
                header = await read_eval_log_async(info, header_only=True)
            except Exception as ex:
                return UnreadableLog(
                    location=info.name, reason=f"{type(ex).__name__}: {ex}"
                )
        return _attempt(info, header)

    results = await asyncio.gather(*(read(info) for info in infos))

    attempts: dict[str, list[LogAttempt]] = {}
    unreadable: list[UnreadableLog] = []
    for result in results:
        if isinstance(result, UnreadableLog):
            unreadable.append(result)
        else:
            attempts.setdefault(result.identifier, []).append(result)

    for identifier in attempts:
        attempts[identifier].sort(key=lambda attempt: attempt.created, reverse=True)

    return ObservedLogs(
        log_dir=log_dir,
        attempts=attempts,
        unreadable=sorted(unreadable, key=lambda log: log.location),
    )


def observe_logs(
    log_dir: str | Path,
    *,
    concurrency: int = DEFAULT_READ_CONCURRENCY,
) -> ObservedLogs:
    """Read a log directory into attempts grouped by task identifier.

    Args:
        log_dir: Directory to read. A directory that does not exist is an empty observation.
        concurrency: Headers to read at once.

    Returns:
        Attempts per identifier and one entry per file that could not be read.
    """
    return asyncio.run(observe_logs_async(log_dir, concurrency=concurrency))


def observe_tasks(manifest: Manifest, logs: ObservedLogs) -> ObservedTasks:
    """Classify a manifest's tasks against a log directory.

    Pure: no clock, no filesystem, no processes.

    Args:
        manifest: Desired state, read from the definition.
        logs: The log directory, as `observe_logs` read it.

    Returns:
        One observation per manifest task, in manifest order, followed by one per identifier found in the directory that the manifest does not name.
    """
    tasks = [_observe_task(task, logs) for task in manifest.tasks]

    named = {task.identifier for task in manifest.tasks}
    tasks.extend(
        TaskObservation(
            identifier=identifier,
            state=TaskState.ORPHANED,
            task=None,
            current=logs.current(identifier),
            superseded=logs.superseded(identifier),
        )
        for identifier in sorted(logs.attempts)
        if identifier not in named
    )

    return ObservedTasks(tasks=tasks, unreadable=logs.unreadable)


def _observe_task(task: ManifestTask, logs: ObservedLogs) -> TaskObservation:
    required = task.samples * task.epochs
    current = logs.current(task.identifier)
    if current is None:
        return TaskObservation(
            identifier=task.identifier,
            state=TaskState.MISSING,
            task=task,
            required_samples=required,
        )

    reason = _incomplete_reason(current, required)
    return TaskObservation(
        identifier=task.identifier,
        state=TaskState.COMPLETE if reason is None else TaskState.INCOMPLETE,
        task=task,
        reason=reason,
        current=current,
        superseded=logs.superseded(task.identifier),
        required_samples=required,
    )


def _incomplete_reason(
    attempt: LogAttempt, required_samples: int
) -> IncompleteReason | None:
    """Why more work is needed, or `None` when it is not.

    The same four conditions `eval_set()` applies (`list_latest_eval_logs`), with the manifest's per-task `samples` and `epochs` standing in for the `ResolvedTask` upstream's `log_samples_complete` resolves them from.
    """
    if attempt.status == "started":
        return IncompleteReason.STARTED
    if attempt.status == "error":
        return IncompleteReason.ERROR
    if attempt.status == "cancelled":
        return IncompleteReason.CANCELLED
    if attempt.invalidated:
        return IncompleteReason.INVALIDATED
    if attempt.total_samples == 0 and required_samples > 0:
        return IncompleteReason.NO_RESULTS
    if attempt.total_samples < required_samples:
        return IncompleteReason.SHORT
    return None


def _attempt(info: EvalLogInfo, header: EvalLog) -> LogAttempt:
    results = header.results
    return LogAttempt(
        location=info.name,
        identifier=task_identifier(header, None),
        created=header.eval.created,
        status=header.status,
        invalidated=header.invalidated,
        error=header.error.message if header.error is not None else None,
        total_samples=results.total_samples if results is not None else 0,
        completed_samples=results.completed_samples if results is not None else 0,
        epochs=header.eval.config.epochs,
        task=header.eval.task,
        task_id=header.eval.task_id,
        eval_id=header.eval.eval_id,
        mtime=info.mtime,
    )


def summarize_states(tasks: Sequence[TaskObservation]) -> dict[str, int]:
    """Count observations by state, for a status line.

    Args:
        tasks: Observations, as `observe_tasks` returns them.

    Returns:
        Counts keyed by state name, including states with no members so the shape is stable.
    """
    counts = {state.value: 0 for state in TaskState}
    for task in tasks:
        counts[task.state.value] += 1
    return counts
