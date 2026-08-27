"""The decision. Given what should be running and what is, what to do next.

Everything before this reads; this is the step that decides. `reconcile` takes desired state (the manifest), what is running (the in-flight record, already resolved to live and departed), and what has happened (a log directory observation), and returns the actions that close the gap.

It is **pure** — no clock, no filesystem, no processes; it reads recorded instants but never asks what time it is — and the design leans on that in three places (execution.md, *The reconcile core, and its drivers*):

- **Testability.** Scheduling correctness becomes "given this state, what actions?", which is a table.
- **Crash recovery is the ordinary path.** There is no resume routine to get wrong: recovery is just the next call, exercised on every tend.
- **`status` is `tend --dry-run`.** The same call with the actions discarded rather than executed — which only holds if computing them has no side effects at all.

What this function decides is mechanical continuity: which workers to spawn and in what order, which departed workers need recording. What a run *means* — whether an error class is systemic, whether an arm is worth continuing — is not here and is not Steward's (execution.md, *What the supervisor decides, and what it escalates*).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from inspect_ai._eval.evalset import TASK_IDENTIFIER_VERSION

from .._evalset.manifest import Manifest
from .._evalset.observe import (
    IncompleteReason,
    LogAttempt,
    ObservedTasks,
    TaskObservation,
    TaskState,
)

DEFAULT_MAX_SAMPLES = 40
"""Starting sample concurrency per task.

Deliberately modest, because the ratchet is asymmetric: raising a limit takes effect immediately, lowering one only stops new acquires and waits for in-flight samples to drain. Climbing from a low setpoint is cheap; descending from a high one is not (scheduling.md, *`max_samples` — set explicitly, so it can be steered*).
"""

DEFAULT_STALL_AFTER = 2
"""Consecutive attempts that may finish nothing new before a task is left alone.

Two rather than one, because a single fruitless attempt is ordinary — a worker killed by a host blip finishes nothing and deserves the retry that a resume makes nearly free. Two in a row is a pattern, and the third would be the first attempt with evidence against it.
"""


class ManifestVersionError(Exception):
    """A manifest whose identifiers cannot be matched against the running inspect.

    Raised rather than reported, because the two inputs are not comparable and every derived number would be wrong in the same direction: unmatchable identifiers make every task read *missing* and every log read *orphaned*, so a finished sweep reads as one that never started. A summary carrying that would look entirely normal, and a caller that forgot to check a flag would re-run a night's compute. An exception cannot be forgotten.
    """


@dataclass(frozen=True)
class Pool:
    """What the operator asked of the worker pool.

    Two knobs that shape the run, and they are about different things. `max_tasks` is *how much runs at once* — the fleet's concurrency, and with `max_samples` its whole load on a provider. `max_workers` is *how many processes that is divided into*, which costs startup and buys crash isolation and changes nothing about how much is in flight. Both are unbounded by default, so a run with neither set puts every task in flight in a process of its own (scheduling.md, *The worker pool*).
    """

    max_workers: int | None = None
    """How many worker processes the run uses, or `None` for a process per task.

    Steward's alone: fanning an eval set across processes is Steward's invention, and no `eval_set()` argument reaches it.
    """

    max_tasks: int | None = None
    """How many tasks may be in flight at once across the whole fleet, or `None` for all of them.

    Distinct from the `max_tasks` Steward writes into a worker's selection, which is that one process's share of this. A definition's own `max_tasks` never reaches a worker, because the selection override is written unconditionally — so this key contradicts nothing the definition can say.
    """

    max_samples: int | None = None
    """Sample concurrency per task, or `None` for no operator preference.

    `None` rather than the default itself, because the two are not the same claim: *no preference* yields to whatever the definition asked for, and a number is an instruction that does not. See `resolve_max_samples`.
    """

    stall_after: int = DEFAULT_STALL_AFTER
    """Consecutive attempts that may finish nothing new before a task stops being respawned.

    Steward's alone for the same reason the two above are: respawning a task is Steward's invention, so there is no `eval_set()` argument this could contradict. See `_stalled`.
    """


@dataclass(frozen=True)
class RunningWorker:
    """A worker confirmed alive.

    The resolved view, not the record: deciding whether a recorded worker is still alive means reading the process table and the control discovery directory, which is I/O and belongs to whatever produces this.
    """

    worker: str
    """Worker stem — the name of its selection document and its output file, and what the record keys on."""

    identifiers: tuple[str, ...]
    """Every task this process is running. One of them at the default width; the whole batch where the run has been packed into fewer processes."""

    pid: int
    host: str

    socket: Path | None = None
    """Control socket, once the worker has bound one.

    `None` means the process exists but has not reached its `eval_set()` boundary yet — the window where it has no log and no discovery entry either. That makes the window a state a summary can report rather than something inferred from absence (execution.md, *The in-flight record is an accelerator*).
    """


@dataclass(frozen=True)
class DepartedWorker:
    """A worker the record accounts for that is no longer running.

    Distinct from `RunningWorker` in one field, and that field is the reason: a worker whose `intent` was written and whose spawn never returned has **no pid**, and inventing one to fit a shared shape would put a number in the record that never named a process.
    """

    worker: str
    identifiers: tuple[str, ...]
    host: str
    pid: int | None = None
    """`None` for a worker that never launched."""


@dataclass(frozen=True)
class InFlight:
    """Workers this host has launched, sorted into live and gone."""

    running: list[RunningWorker] = field(default_factory=list[RunningWorker])
    """Confirmed alive. These occupy a slot and suppress a respawn."""

    departed: list[DepartedWorker] = field(default_factory=list[DepartedWorker])
    """Recorded but no longer alive. These need an `exited` entry and occupy nothing."""

    spent: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    """When each of a task's finished spawn attempts began, however it ended.

    The record's own account, and the only evidence there is about a worker that died **before landing a log** — a definition that will not import, an OOM during startup. Such a task reads `missing` on every tend and would otherwise be respawned forever, invisibly, because nothing in the log directory ever changes to say it was tried.

    Times rather than a count, because the two halves of `_stalled` have to be *merged* rather than chosen between: a task whose history is one partial log and then three crashes has evidence in both places, and only the ordering says whether the crashes came after the progress or before it.
    """

    @property
    def running_identifiers(self) -> set[str]:
        return {
            identifier for worker in self.running for identifier in worker.identifiers
        }

    @property
    def running_tasks(self) -> int:
        """Tasks in flight, which is what `Pool.max_tasks` bounds. Not the same as `len(running)` once a process holds several."""
        return sum(len(worker.identifiers) for worker in self.running)


@dataclass(frozen=True)
class SpawnTask:
    """One task's spawn decision.

    Per task rather than per worker, because every field here is a fact about the task: which log it resumes, how many times it has been tried, why it needs work. A worker holding several of them holds several of each.
    """

    identifier: str

    key: str
    """Display key from the manifest."""

    resume: str | None
    """Location of a prior log to resume, or `None` to start fresh. Completed, non-errored, non-invalidated samples are reused, so a resume of a five-hundred-sample task with forty-seven errors runs forty-seven samples."""

    attempt: int
    """1-based, counting every attempt Steward knows about — the logs already in the directory and, for a worker that died before landing one, the in-flight record's spent attempts. It names the worker, so it has to advance on every spawn even when nothing was left behind."""

    reason: IncompleteReason | None
    """Why more work is needed (`None` when the task has never run)."""


@dataclass(frozen=True)
class SpawnWorker:
    """Run these tasks, in one process.

    Everything a selection document needs, decided but not yet written. One task at the default width; a share of the run where `Pool.max_workers` has packed it into fewer processes.
    """

    tasks: tuple[SpawnTask, ...]
    """What this process runs, all of it concurrently. Never empty."""

    max_samples: int
    """Sample concurrency, applied per task rather than divided among them — inspect's semaphore is per task, so a definition's value passes through unchanged however many tasks a worker holds (scheduling.md, *The three knobs have different scopes*)."""

    @property
    def first(self) -> SpawnTask:
        """The task this worker is named after. Its whole content at the default width."""
        return self.tasks[0]

    @property
    def identifiers(self) -> tuple[str, ...]:
        return tuple(task.identifier for task in self.tasks)


@dataclass(frozen=True)
class ReapWorker:
    """Record that a worker is gone."""

    worker: DepartedWorker


@dataclass(frozen=True)
class ArchiveLog:
    """Move a log out of the run's directory, because nothing in the manifest claims it.

    Never a delete. A log leaving `logs/` is always a move to the sibling archive — reversible, journaled with its reason, and restorable for free if the edit that orphaned it is reverted (workflow.md, *Steward never destroys a result, but it does curate the directory*).

    **Mechanical here because the gate is somewhere else.** Archiving is the one action that removes a result from view, and a one-character change to a task arg reads identically to a deliberate removal — so it is gated on explicit acceptance at the moment the manifest is *committed*, where a human is present and the delta can be shown. Once desired state says a task is not in the eval set, converging toward it is bookkeeping.
    """

    location: str
    """The log to move, as `observe_logs` reported it."""

    identifier: str
    """Task identifier the log belongs to, for the journal."""


Action = SpawnWorker | ReapWorker | ArchiveLog


@dataclass(frozen=True)
class Summary:
    """Where the run stands, as data.

    Rendering is somebody else's: the same summary becomes a tend's stdout, `status.md`, and a journal entry.
    """

    tasks: int
    """Tasks in the manifest."""

    states: dict[str, int]
    """Counts by `TaskState`, including states with no members so the shape is stable."""

    reasons: dict[str, int]
    """Counts by `IncompleteReason`, over incomplete tasks only."""

    running: int
    """Tasks in flight, not processes. The two differ once a run is packed, and this is the one `max_tasks` bounds."""

    workers: int
    """Processes alive. Equal to `running` at the default width."""

    spawning: int
    """Tasks starting this turn."""

    spawning_workers: int
    """Processes starting this turn."""

    queued: int

    stalled: list[str]
    """Identifiers left alone because respawning them has stopped accomplishing anything (`_stalled`). Work that needs a person, and the one thing in this summary nothing mechanical will resolve."""

    orphans: list[str]
    """Identifiers in the log directory that the manifest does not name. Archived as the manifest converges, except for any still running."""

    orphans_running: list[str]
    """Orphaned identifiers that still have a live worker. Left alone entirely — neither archived nor stopped, since stopping a worker is not a mechanical act."""

    archiving: int
    """Logs being moved to the archive this turn."""

    unreadable: int
    """Files in the log directory that could not be read as logs."""

    max_workers: int | None
    """Process count the operator asked for, or `None` for a process per task."""

    max_tasks: int | None
    """Tasks the operator will allow in flight, or `None` for all of them."""

    blocked: "Blocked | None"
    """Which bound is holding `queued` back — the one worth raising, not merely the one that is set. `None` when nothing is queued, and also when the queue is a pause rather than a limit."""

    paused: bool


class Blocked(StrEnum):
    """Which of the two bounds is holding the queue back.

    Named rather than derived from the settings, because *configured* and *binding* are different questions: a run with both keys set is often short of one and nowhere near the other, and pointing its operator at the wrong one costs them a turn to find out.
    """

    MAX_TASKS = "max_tasks"
    """The fleet is at its task ceiling. Raising it starts more work."""

    MAX_WORKERS = "max_workers"
    """Every process the run is allowed is already alive. Raising `max_tasks` changes nothing until one exits or `max_workers` goes up."""


@dataclass(frozen=True)
class Poured:
    """How the pending work divided, and what stopped the rest from starting."""

    workers: list[tuple[SpawnTask, ...]] = field(
        default_factory=list[tuple[SpawnTask, ...]]
    )
    """One tuple per process to spawn."""

    queued: list[SpawnTask] = field(default_factory=list[SpawnTask])
    """Tasks that could not start this turn, in spawn order."""

    blocked: "Blocked | None" = None
    """What is holding `queued` back, or `None` when nothing is queued."""


@dataclass(frozen=True)
class Reconciliation:
    """What to do, what is waiting, and where things stand."""

    actions: list[Action]
    """To execute in order. A `status` computes these and throws them away."""

    queued: list[SpawnTask]
    """Would spawn, but the run's shape has no room. The same decision deferred, which is what lets an authorized re-run jump the queue by sorting rather than by a second code path (scheduling.md, *Approved re-runs go first*).

    Tasks rather than workers, because which process a task ends up in is decided by the pour at the moment it is placed — a queued task has no worker yet.
    """

    summary: Summary


def reconcile(
    manifest: Manifest,
    inflight: InFlight,
    observed: ObservedTasks,
    *,
    pool: Pool,
    paused: bool = False,
) -> Reconciliation:
    """Decide what to do next.

    Args:
        manifest: Desired state, captured from the definition.
        inflight: Workers this host launched, resolved into live and departed.
        observed: The log directory read against `manifest`.
        pool: The run's shape — how many tasks may be in flight, how many processes to divide them into, and the default sample concurrency.
        paused: Stop scheduling new work. Workers already running finish normally — this is what almost everyone means by pausing a run, and it needs no control channel at all (workflow.md, *What `pause` actually pauses*).

    Returns:
        Actions to execute, tasks waiting on a slot, and a summary.

    Raises:
        ManifestVersionError: If the manifest's identifiers were computed by a different `task_identifier` version than the running inspect_ai's.
    """
    if manifest.identifier_version != TASK_IDENTIFIER_VERSION:
        raise ManifestVersionError(
            f"This manifest's task identifiers were computed by task_identifier "
            f"version {manifest.identifier_version}, but the installed inspect_ai "
            f"uses version {TASK_IDENTIFIER_VERSION}. Nothing in the log directory "
            f"can be matched to it, so the whole run would read as not yet started. "
            f"Re-capture with `steward launch`."
        )

    running = inflight.running_identifiers
    max_samples = resolve_max_samples(manifest, pool)

    pending: list[SpawnTask] = []
    stalled: list[str] = []
    for observation in _spawn_order(observed.tasks):
        if observation.identifier in running:
            continue
        if _stalled(observation, inflight, pool.stall_after):
            stalled.append(observation.identifier)
        else:
            pending.append(_spawn(observation, inflight))

    poured = (
        Poured(queued=pending)
        if paused
        else pour(
            pending,
            pool=pool,
            tasks_running=inflight.running_tasks,
            workers_running=len(inflight.running),
        )
    )
    queued = poured.queued
    spawning = [
        SpawnWorker(tasks=batch, max_samples=max_samples) for batch in poured.workers
    ]

    # a paused run makes no changes to itself, and a move is a change. Reaping
    # is not: it records what already happened, which stays true either way
    archiving = [] if paused else _archiving(observed, running)

    actions: list[Action] = [ReapWorker(worker) for worker in inflight.departed]
    actions.extend(archiving)
    actions.extend(spawning)

    return Reconciliation(
        actions=actions,
        queued=queued,
        summary=_summarize(
            observed,
            running=running,
            workers=len(inflight.running),
            spawning=spawning,
            queued=len(queued),
            blocked=poured.blocked,
            stalled=stalled,
            archiving=len(archiving),
            pool=pool,
            paused=paused,
        ),
    )


def pour(
    pending: list[SpawnTask],
    *,
    pool: Pool,
    tasks_running: int,
    workers_running: int,
) -> Poured:
    """Divide the tasks that can start now among the processes that will run them.

    Two independent bounds, and reading them as one is the mistake this shape exists to prevent. `max_tasks` decides **how much starts** — it is the fleet's concurrency and, with `max_samples`, its whole load on a provider. `max_workers` decides **how few processes that is divided into** — it costs nothing in concurrency and buys back the per-process startup a frontend charges, which for a Hawk config is an install and a secrets round trip per worker. Either unset is unbounded, so a run with neither puts every pending task in flight in a process of its own.

    **Dealt round-robin rather than sliced.** `_spawn_order` transposes the enumeration to task-major, so consecutive entries are the same task across models; handing a process a contiguous run of them would put every model of one task in one process, and that process dying would cost the task on every arm at once — the uncomparable interruption that ordering exists to prevent. Dealing spreads each task's models across processes instead.

    **Which bound held is decided here rather than inferred from the settings**, because the two are not the same question and only this function knows the answer. A run with both keys set can be short of processes while well under `max_tasks`, and telling its operator to raise `max_tasks` would send them to a number that changes nothing.

    Args:
        pending: Tasks needing work, in spawn order.
        pool: The run's shape.
        tasks_running: Tasks already in flight, which `max_tasks` counts against.
        workers_running: Processes already alive, which `max_workers` counts against.

    Returns:
        One tuple of tasks per process to spawn, the tasks left waiting, and what they are waiting on.
    """
    placeable = (
        len(pending)
        if pool.max_tasks is None
        else max(0, pool.max_tasks - tasks_running)
    )
    batch, queued = pending[:placeable], pending[placeable:]
    if not batch:
        # nothing may start: either `max_tasks` is spent, or there was nothing
        # pending to begin with, which is a queue of zero and no bound at all
        return Poured(queued=queued, blocked=Blocked.MAX_TASKS if queued else None)

    slots = (
        len(batch)
        if pool.max_workers is None
        else max(0, pool.max_workers - workers_running)
    )
    processes = min(slots, len(batch))
    if processes == 0:
        # every process the run is allowed is already alive. Note that its
        # tasks go back on the queue rather than joining a live worker: a
        # selection document is written once, at spawn, and a running worker
        # cannot be given more work.
        #
        # `max_workers` is named even where `max_tasks` also truncated, and
        # that is the useful answer rather than the complete one: raising
        # `max_tasks` while no process can be started buys nothing
        return Poured(queued=pending, blocked=Blocked.MAX_WORKERS)

    dealt: list[list[SpawnTask]] = [[] for _ in range(processes)]
    for index, task in enumerate(batch):
        dealt[index % processes].append(task)
    return Poured(
        workers=[tuple(tasks) for tasks in dealt],
        queued=queued,
        blocked=Blocked.MAX_TASKS if queued else None,
    )


def _archiving(observed: ObservedTasks, running: set[str]) -> list[ArchiveLog]:
    """Every log of every orphan, except the orphans something is still running.

    All of an orphan's attempts rather than only its current one: the identifier has left the definition entirely, so there is no attempt history left to reason about and leaving the superseded ones behind would defeat the point, which is that `logs/` holds the current definition's results and nothing else.
    """
    return [
        ArchiveLog(location=attempt.location, identifier=task.identifier)
        for task in observed.tasks
        if task.state == TaskState.ORPHANED and task.identifier not in running
        for attempt in _attempts(task)
    ]


def _attempts(observation: TaskObservation) -> list[LogAttempt]:
    """Every log for a task, oldest first."""
    attempts = list(observation.superseded)
    if observation.current is not None:
        attempts.append(observation.current)
    return sorted(attempts, key=lambda attempt: attempt.created)


def _stalled(
    observation: TaskObservation, inflight: InFlight, stall_after: int
) -> bool:
    """Whether respawning this task has stopped accomplishing anything.

    `SpawnWorker` is the one action in this vocabulary that is not **convergent**: nothing else here can repeat forever, and without a guard a task that fails the same way every time is respawned every ten minutes until someone notices. It lands precisely on the likeliest case — a task with permanently-failing samples is `SHORT` forever, resumes forever, and completes nothing new each time.

    **The signal is progress, not attempt count**, and the difference is the whole point: a task on its fourth attempt with 490 of 500 samples done is converging and should continue, while one repeating the same twelve failures is not. So an attempt counts as progress when it finishes more samples than every attempt before it, and the guard fires on a run of attempts that did not.

    **A worker that leaves no log is the other half**, and it needs the record rather than the directory. One that dies before its `eval_set()` boundary — a definition that will not import, an OOM during startup — leaves nothing behind, so the task reads exactly as it did before it was tried. Roadmap calls that the probable failure of a large sweep.

    **The two halves merge rather than alternate**, and consulting the record only for a task with no logs at all was a bug: a task whose first attempt lands a partial log and whose next twenty die at import has evidence in both places, and reading only the log would see one attempt that made progress and respawn forever. So the spent attempts that began *after the newest log did* are folded into the same run. That test identifies exactly the attempts that landed nothing, since an attempt that landed a log would be the newest one itself.

    **An invalidation clears the history before it, and only that.** Somebody reached in and marked samples for a re-run, which is a decision to try again made by the only party entitled to make one — so nothing that happened before they acted counts against the task any more. What they cannot do is exempt it forever: a retry that dies before landing a replacement leaves the invalidated log current, so *returning* here rather than resetting made the one branch with no ceiling on it, and an import error under an invalidated log respawned every ten minutes for as long as the run lasted. Attempts since the invalidation count normally.
    """
    attempts = _attempts(observation)
    spent = inflight.spent.get(observation.identifier, [])

    if observation.reason == IncompleteReason.INVALIDATED:
        # when they acted, as closely as anything records it: invalidating
        # rewrites the log, and nothing else touches a finished one. Without a
        # time there is nothing to count from, so the task gets the benefit
        since = _mtime(observation.current)
        return since is not None and _after(spent, since) >= stall_after

    if not attempts:
        return len(spent) >= stall_after

    best = 0
    fruitless = 0
    for attempt in attempts:
        if attempt.completed_samples > best:
            best = attempt.completed_samples
            fruitless = 0
        else:
            fruitless += 1

    if (newest := _when(attempts[-1].created)) is not None:
        fruitless += _after(spent, newest)
    return fruitless >= stall_after


def _after(spent: list[str], instant: datetime) -> int:
    """How many of a task's finished attempts began after some instant.

    Which is how many of them landed no log, whenever `instant` comes from the newest log there is: an attempt that landed one would be newer than the log it is being compared against.
    """
    return sum(
        1
        for started in spent
        if (when := _when(started)) is not None and when > instant
    )


def _mtime(attempt: LogAttempt | None) -> datetime | None:
    """When a log was last written, as an instant.

    `LogAttempt.mtime` is in milliseconds — `EvalLogInfo` normalizes every backend's answer that way — and a value that will not convert yields `None` rather than an exception, on the same principle as `_when`: this is a guard, and it may not be the thing that fails a turn.
    """
    if attempt is None or attempt.mtime is None:
        return None
    try:
        return datetime.fromtimestamp(attempt.mtime / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _when(value: str) -> datetime | None:
    """One recorded instant, comparable with any other.

    Two formats meet here — a log's `created` and the record's `ts` — and they are written by different code with different offset conventions, so comparing the strings would be comparing `Z` against `+00:00`. A value that will not parse yields `None` and is simply not counted, which loses a stall rather than inventing one.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _spawn_order(tasks: list[TaskObservation]) -> list[TaskObservation]:
    """Tasks needing work, ordered task-major.

    Enumeration arrives **model-major** — `eval_resolve_tasks` loops models on the outside — and spawning in that order is the worst available choice: interrupt the run halfway and one model is complete while the other is untouched, so the arms cannot be compared at all; and one rate-limit bucket is saturated while the others idle.

    The fix is a *transposition*, not a sort, which is what makes it safe to apply unasked. The user wrote a task order and a model order; the nesting is `eval_resolve_tasks`'s choice, not theirs, and flipping the axis preserves both sequences exactly as written. It also puts one task per model in flight whenever the ceiling is at least the model count — the stratification `eval_set()` buys with a second knob (scheduling.md, *Spawn order transposes the crossing*).

    Grouping is on the identifier's own prefix components — file, name, args hash — taken as manifest fields rather than by parsing the identifier string.
    """
    pending = [
        task
        for task in tasks
        if task.state in (TaskState.MISSING, TaskState.INCOMPLETE)
    ]

    first_seen: dict[tuple[str | None, str, str], int] = {}
    for task in pending:
        assert task.task is not None, "a pending task always has a manifest row"
        first_seen.setdefault(
            (task.task.file, task.task.name, task.task.args_hash), len(first_seen)
        )

    def group(task: TaskObservation) -> int:
        assert task.task is not None
        return first_seen[(task.task.file, task.task.name, task.task.args_hash)]

    # stable, so the author's model order survives within each task group
    return sorted(pending, key=group)


def _spawn(observation: TaskObservation, inflight: InFlight) -> SpawnTask:
    """One task's spawn decision.

    A task with a prior log resumes it whatever went wrong — short, errored, cancelled, invalidated, or a worker that died mid-run. There is deliberately no branch on the reason: resume reuses exactly the samples that are worth keeping, so the reason is reporting material rather than an input to the decision.
    """
    current = observation.current
    return SpawnTask(
        identifier=observation.identifier,
        key=observation.key,
        resume=current.location if current is not None else None,
        attempt=_attempt(observation, inflight),
        reason=observation.reason,
    )


def attempts_made(observation: TaskObservation, inflight: InFlight) -> int:
    """How many tries this task has already had, from both records that know.

    The larger of the two counts rather than their sum, because an attempt that landed a log is in both. That makes this the log count when every attempt landed one, the spent count when none did, and never less than either.

    Two callers, which is why it is one function: `_attempt` numbers the next try from it, and a stalled task's item id is keyed on it so that a further failed attempt is a *new* item rather than one already acknowledged.

    Args:
        observation: The task's logs.
        inflight: The record of attempts that landed none.

    Returns:
        Attempts made so far, zero for a task that has never run.
    """
    landed = len(observation.superseded) + (1 if observation.current is not None else 0)
    return max(landed, len(inflight.spent.get(observation.identifier, [])))


def _attempt(observation: TaskObservation, inflight: InFlight) -> int:
    """Which try this is, 1-based, counting every one Steward knows about.

    **It is an estimate, not a guarantee of uniqueness.** Both counts can be lost — `.steward/` is a directory the design tells people they may delete — and two landed logs plus a discarded record would number the next attempt 3 twice. The worker stem cannot tolerate that, so `Fleet.spawn` resolves it against the directory it is about to write into; here the number means *which try this is*, and there it also has to be a name.
    """
    return attempts_made(observation, inflight) + 1


def resolve_max_samples(manifest: Manifest, pool: Pool) -> int:
    """Sample concurrency for a worker: three sources, most specific first.

    | | |
    |---|---|
    | the **operator's** `Pool.max_samples` | somebody typed a number for this run, so nothing outranks it |
    | the **definition's** `max_samples` | how many samples a task should run at once is a property of the eval, and its author knows the workload |
    | `DEFAULT_MAX_SAMPLES` | nobody expressed a preference |

    The distinction between the first two is why `Pool.max_samples` is optional rather than pre-filled with the default. Collapsing them gets it wrong in one direction or the other — Steward's own fallback silently outranking a definition, or an explicit operator instruction silently losing to one — and neither is visible from the resulting number.

    Whichever wins is written into the selection explicitly. That is a starting point rather than a ceiling: step 21's tuning loop changes where a worker ends up, not where it begins.

    Args:
        manifest: Captured desired state.
        pool: What the operator asked for.

    Returns:
        Sample concurrency for every worker this reconcile spawns.
    """
    if pool.max_samples is not None:
        return pool.max_samples

    # options is free-form and deserialized, so a manifest written by another
    # version can carry anything here; a definition that set nothing carries None
    requested: Any = manifest.options.get("max_samples")
    return (
        requested
        if isinstance(requested, int) and requested > 0
        else DEFAULT_MAX_SAMPLES
    )


def _summarize(
    observed: ObservedTasks,
    *,
    running: set[str],
    workers: int,
    spawning: list[SpawnWorker],
    queued: int,
    blocked: "Blocked | None",
    stalled: list[str],
    archiving: int,
    pool: Pool,
    paused: bool,
) -> Summary:
    states = {state.value: 0 for state in TaskState}
    reasons = {reason.value: 0 for reason in IncompleteReason}
    orphans: list[str] = []
    for task in observed.tasks:
        states[task.state.value] += 1
        if task.reason is not None:
            reasons[task.reason.value] += 1
        if task.state == TaskState.ORPHANED:
            orphans.append(task.identifier)

    return Summary(
        tasks=sum(1 for task in observed.tasks if task.state != TaskState.ORPHANED),
        states=states,
        reasons=reasons,
        running=len(running),
        workers=workers,
        spawning=sum(len(worker.tasks) for worker in spawning),
        spawning_workers=len(spawning),
        queued=queued,
        stalled=stalled,
        orphans=orphans,
        orphans_running=[identifier for identifier in orphans if identifier in running],
        archiving=archiving,
        unreadable=len(observed.unreadable),
        max_workers=pool.max_workers,
        max_tasks=pool.max_tasks,
        blocked=blocked,
        paused=paused,
    )
