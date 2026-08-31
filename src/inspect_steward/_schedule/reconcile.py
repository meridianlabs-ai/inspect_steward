"""The decision. Given what should be running and what is, what to do next.

Everything before this reads; this is the step that decides. `reconcile` takes desired state (the manifest), what is running (the in-flight record, already resolved to live and departed), and what has happened (a log directory observation), and returns the actions that close the gap.

It is **pure** — no clock, no filesystem, no processes; it reads recorded instants but never asks what time it is — and the design leans on that in three places (execution.md, *The reconcile core, and its drivers*):

- **Testability.** Scheduling correctness becomes "given this state, what actions?", which is a table.
- **Crash recovery is the ordinary path.** There is no resume routine to get wrong: recovery is just the next call, exercised on every tend.
- **`status` is `tend --dry-run`.** The same call with the actions discarded rather than executed — which only holds if computing them has no side effects at all.

What this function decides is mechanical continuity: which workers to spawn and in what order, which departed workers need recording. What a run *means* — whether an error class is systemic, whether an arm is worth continuing — is not here and is not Steward's (execution.md, *What the supervisor decides, and what it escalates*).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

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

Also the default ramp's floor, and the two agree by construction: a run that starts at 40 and never earns a step is exactly the run this constant always described.
"""

DEFAULT_SAMPLES_RAMP = (DEFAULT_MAX_SAMPLES, 200)
"""The range the tuning loop explores when nobody pinned a setpoint or wrote a range.

On by default, which withdraws an earlier position deliberately (scheduling.md, *The signal is mechanical*): a run left alone at 40 all night compounds its undershoot for exactly the hours Steward exists to cover, and every step up is gated on measured absence of pushback where staying low is gated on nothing. The ceiling is a bound on discovery, not a promise of load — a run that never earns a step never leaves the floor.
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

    **What this carries is what the *operator* asked for, which is not the same as what is in force.** Only `max_workers` and `stall_after` resolve here, because `_steward.yaml` is their source. The two knobs the definition can also express — `max_tasks` and `max_samples` — carry the command line's value or `None`, and their chains finish in `resolve_max_tasks` and `resolve_max_samples`, which have the manifest to ask.
    """

    max_workers: int | None = None
    """How many worker processes the run uses, or `None` for a process per task.

    Steward's alone: fanning an eval set across processes is Steward's invention, and no `eval_set()` argument reaches it.
    """

    max_tasks: int | None = None
    """Fleet width from the command line, or `None` for no operator preference.

    `None` rather than a number for the same reason `max_samples` is: *no preference* yields to whatever the definition asked for, and a number is an instruction that does not. See `resolve_max_tasks`.

    Distinct from the `max_tasks` Steward writes into a worker's selection, which is that one process's share of the fleet's width rather than the width itself.
    """

    max_samples: int | None = None
    """Sample concurrency per task, or `None` for no operator preference.

    `None` rather than the default itself, because the two are not the same claim: *no preference* yields to whatever the definition asked for, and a number is an instruction that does not. See `resolve_max_samples`.
    """

    samples_ramp: tuple[int, int] | Literal[False] | None = None
    """The range the tuning loop may explore, `False` to disable it, or `None` for the default.

    From `_steward.yaml`, and consulted only when nothing pinned a setpoint: an explicit `max_samples` — this pool's or the definition's — switches the whole policy off, which is what keeps the key from ever contradicting a definition. See `resolve_samples_ramp`.
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

    started: str = ""
    """When the spawn began, from the in-flight record — what lets a departure with no log say when the attempt it wasted was."""


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

    registry_name: str | None = None
    """The task's registered name, carried through to the selection so a worker can skip constructing the tasks it was not given. `None` for an orphan, which has no manifest row to read it from, and for an ad-hoc task, which has no registered name to have."""

    args_hash: str | None = None
    """The task's argument hash, paired with `registry_name`. From the manifest rather than recomputed: the whole point is that it agrees with what capture wrote."""


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

    capture_rss: int | None
    """Peak memory of the capture that read this definition, in bytes, or `None` where nothing measured it.

    Carried so a reader can be told what the run's *width* costs without re-reading the manifest. A ceiling on a worker's startup rather than an estimate of it, since capture builds every task and a worker builds its own — see `_evalset/cost.py`.
    """

    paused: bool

    rerunning: int = 0
    """Authorized re-runs among the tasks spawning and queued — invalidated, or covered by a standing `rerun` ruling. The counts line's account of why these go first (scheduling.md §5.5)."""


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

    warnings: list[str] = field(default_factory=list[str])
    """Recorded instants the stall guard could not read, named per task.

    `_stalled` treats an unparseable time as *not evidence* — losing a stall rather than inventing one — which is the right refusal and a quiet one: the guard is weaker than it looks for exactly the tasks whose records are damaged, and nothing said so. Reported rather than logged, because this function is pure; an executing turn writes them to `steward.log`, and a `status` discards them with the actions.
    """


def reconcile(
    manifest: Manifest,
    inflight: InFlight,
    observed: ObservedTasks,
    *,
    pool: Pool,
    paused: bool = False,
    levels: Mapping[str, int] | None = None,
    ruled: Mapping[str, str] | None = None,
) -> Reconciliation:
    """Decide what to do next.

    Args:
        manifest: Desired state, captured from the definition.
        inflight: Workers this host launched, resolved into live and departed.
        observed: The log directory read against `manifest`.
        pool: The run's shape — how many tasks may be in flight, how many processes to divide them into, and the default sample concurrency.
        paused: Stop scheduling new work. Workers already running finish normally — this is what almost everyone means by pausing a run, and it needs no control channel at all (workflow.md, *What `pause` actually pauses*).
        levels: Where the tuning loop has already climbed each task's sample concurrency, by identifier. A respawn spawns at its task's level rather than restarting at the floor — the climb was earned against measured headroom, and a worker crash does not unmeasure it. Consulted only while a ramp is active, so a pinned run cannot be moved by stale levels.
        ruled: Tasks a standing `rerun` ruling covers, each with the ruling's instant (ISO), folded by the turn. Two effects, both the ruling's application: the stall guard forgives attempt history at or before the instant — the decision to try again was made by the only party entitled to make one — and the covered tasks sort ahead of fresh work, by queue order rather than preemption (scheduling.md §5.5).

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
    width = resolve_max_tasks(manifest, pool)
    ruled = ruled or {}

    pending: list[SpawnTask] = []
    stalled: list[str] = []
    warnings: list[str] = []
    for observation in _spawn_order(observed.tasks):
        if observation.identifier in running:
            continue
        if _stalled(
            observation,
            inflight,
            pool.stall_after,
            warnings,
            ruled_ts=ruled.get(observation.identifier),
        ):
            stalled.append(observation.identifier)
        else:
            pending.append(_spawn(observation, inflight))

    # authorized re-runs go first: a stable partition, so the queue order does
    # the promoting and nothing is ever preempted (scheduling.md §5.5). Within
    # each half the spawn order stands
    def authorized(task: SpawnTask) -> bool:
        return task.reason is IncompleteReason.INVALIDATED or task.identifier in ruled

    pending = [task for task in pending if authorized(task)] + [
        task for task in pending if not authorized(task)
    ]

    poured = (
        Poured(queued=pending)
        if paused
        else pour(
            pending,
            pool=pool,
            max_tasks=width,
            tasks_running=inflight.running_tasks,
            workers_running=len(inflight.running),
        )
    )
    queued = poured.queued
    ramp = resolve_samples_ramp(manifest, pool)
    spawning = [
        SpawnWorker(
            tasks=batch,
            max_samples=_spawn_level(
                batch, max_samples, levels if ramp and levels else {}, ramp
            ),
        )
        for batch in poured.workers
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
        warnings=warnings,
        summary=_summarize(
            observed,
            running=running,
            workers=len(inflight.running),
            spawning=spawning,
            queued=len(queued),
            blocked=poured.blocked,
            capture_rss=manifest.source.capture_rss,
            stalled=stalled,
            archiving=len(archiving),
            pool=pool,
            max_tasks=width,
            paused=paused,
            rerunning=sum(
                1
                for task in [
                    *(task for batch in poured.workers for task in batch),
                    *queued,
                ]
                if authorized(task)
            ),
        ),
    )


def pour(
    pending: list[SpawnTask],
    *,
    pool: Pool,
    max_tasks: int | None,
    tasks_running: int,
    workers_running: int,
) -> Poured:
    """Divide the tasks that can start now among the processes that will run them.

    Two independent bounds, and reading them as one is the mistake this shape exists to prevent. `max_tasks` decides **how much starts** — it is the fleet's concurrency and, with `max_samples`, its whole load on a provider. `max_workers` decides **how few processes that is divided into** — it costs nothing in concurrency and buys back the per-process startup a frontend charges, which for a Hawk config is an install and a secrets round trip per worker. Either unset is unbounded, so a run with neither puts every pending task in flight in a process of its own.

    **Dealt round-robin rather than sliced.** `_spawn_order` transposes the enumeration to task-major, so consecutive entries are the same task across models; handing a process a contiguous run of them would put every model of one task in one process, and that process dying would cost the task on every arm at once — the uncomparable interruption that ordering exists to prevent. Dealing spreads each task's models across processes instead.

    **Which bound held is decided here rather than inferred from the settings**, because the two are not the same question and only this function knows the answer. A run with both keys set can be short of processes while well under `max_tasks`, and telling its operator to raise `max_tasks` would send them to a number that changes nothing.

    Args:
        pending: Tasks needing work, in spawn order.
        pool: The run's shape. Only `max_workers` is read from it — fleet width arrives resolved, since its sources are the command line and the definition rather than anything `Pool` carries alone.
        max_tasks: Fleet width in force, from `resolve_max_tasks`, or `None` for unbounded.
        tasks_running: Tasks already in flight, which `max_tasks` counts against.
        workers_running: Processes already alive, which `max_workers` counts against.

    Returns:
        One tuple of tasks per process to spawn, the tasks left waiting, and what they are waiting on.
    """
    placeable = len(pending) if max_tasks is None else max(0, max_tasks - tasks_running)
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
    observation: TaskObservation,
    inflight: InFlight,
    stall_after: int,
    warnings: list[str],
    ruled_ts: str | None = None,
) -> bool:
    """Whether respawning this task has stopped accomplishing anything.

    `SpawnWorker` is the one action in this vocabulary that is not **convergent**: nothing else here can repeat forever, and without a guard a task that fails the same way every time is respawned every ten minutes until someone notices. It lands precisely on the likeliest case — a task with permanently-failing samples is `SHORT` forever, resumes forever, and completes nothing new each time.

    **The signal is progress, not attempt count**, and the difference is the whole point: a task on its fourth attempt with 490 of 500 samples done is converging and should continue, while one repeating the same twelve failures is not. So an attempt counts as progress when it finishes more samples than every attempt before it, and the guard fires on a run of attempts that did not.

    **A worker that leaves no log is the other half**, and it needs the record rather than the directory. One that dies before its `eval_set()` boundary — a definition that will not import, an OOM during startup — leaves nothing behind, so the task reads exactly as it did before it was tried. Roadmap calls that the probable failure of a large sweep.

    **The two halves merge rather than alternate**, and consulting the record only for a task with no logs at all was a bug: a task whose first attempt lands a partial log and whose next twenty die at import has evidence in both places, and reading only the log would see one attempt that made progress and respawn forever. So the spent attempts that began *after the newest log did* are folded into the same run. That test identifies exactly the attempts that landed nothing, since an attempt that landed a log would be the newest one itself.

    **An invalidation clears the history before it, and only that.** Somebody reached in and marked samples for a re-run, which is a decision to try again made by the only party entitled to make one — so nothing that happened before they acted counts against the task any more. What they cannot do is exempt it forever: a retry that dies before landing a replacement leaves the invalidated log current, so *returning* here rather than resetting made the one branch with no ceiling on it, and an import error under an invalidated log respawned every ten minutes for as long as the run lasted. Attempts since the invalidation count normally.

    **A rerun ruling forgives everything at or before it, and only that.** The same shape as the invalidation clause below, reached one layer up: a person (or a standing policy) ruled the class re-runnable, which is a decision to try again by the only party entitled to make one — so the attempts before the ruling stop counting, the first post-ruling attempt starts a fresh progress baseline, and `stall_after` fresh fruitless attempts re-stall (errors.qmd, the cleared-history doctrine). The forgiveness instant is the later of the ruling and the invalidation's own write time, since the applier's invalidation always postdates the ruling it applies.

    **An instant that will not read is reported, never counted.** Every unparseable time here weakens the guard in the lenient direction — losing a stall rather than inventing one — and that used to be silent, so a task whose record was damaged looked exactly like one converging. `warnings` collects one line per skipped instant, for the executing turn to log.
    """
    attempts = _attempts(observation)
    spent = inflight.spent.get(observation.identifier, [])

    if ruled_ts is not None:
        return _stalled_since_ruling(
            observation, attempts, spent, stall_after, warnings, ruled_ts
        )

    if observation.reason == IncompleteReason.INVALIDATED:
        # when they acted, as closely as anything records it: invalidating
        # rewrites the log, and nothing else touches a finished one. Without a
        # time there is nothing to count from, so the task gets the benefit
        since = _mtime(observation.current)
        if since is None:
            warnings.append(
                f"the invalidated log for {observation.key} has no readable "
                f"write time, so the stall guard is counting no attempts "
                f"against it"
            )
            return False
        return _after(spent, since, observation.key, warnings) >= stall_after

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
        fruitless += _after(spent, newest, observation.key, warnings)
    else:
        warnings.append(
            f"the newest log for {observation.key} has an unreadable created "
            f"time ({attempts[-1].created!r}), so crashed attempts since it "
            f"are not counted toward the stall guard"
        )
    return fruitless >= stall_after


def _stalled_since_ruling(
    observation: TaskObservation,
    attempts: list[LogAttempt],
    spent: list[str],
    stall_after: int,
    warnings: list[str],
    ruled_ts: str,
) -> bool:
    """The stall guard for a task a standing `rerun` ruling covers.

    Attempt history — logs and crashed spawns both — at or before the forgiveness instant is cleared entirely, and the ordinary progress rule runs over what is left with a fresh baseline: the first post-ruling attempt competes against nothing, and `stall_after` fresh fruitless attempts re-stall the task exactly as they would a new one.
    """
    forgiveness = _when(ruled_ts)
    if forgiveness is None:
        warnings.append(
            f"the rerun ruling instant for {observation.key} ({ruled_ts!r}) is "
            f"unreadable, so the stall guard is counting no attempts against it"
        )
        return False
    if observation.reason == IncompleteReason.INVALIDATED:
        # the applier's invalidation postdates the ruling it applies, so the
        # rewrite's own instant is the later, truer edge of the forgiveness
        since = _mtime(observation.current)
        if since is not None and since > forgiveness:
            forgiveness = since

    fresh: list[LogAttempt] = []
    for attempt in attempts:
        when = _when(attempt.created)
        if when is None:
            warnings.append(
                f"a log for {observation.key} has an unreadable created time "
                f"({attempt.created!r}) and is not counted toward the stall guard"
            )
        elif when > forgiveness:
            fresh.append(attempt)

    best = 0
    fruitless = 0
    for attempt in fresh:
        if attempt.completed_samples > best:
            best = attempt.completed_samples
            fruitless = 0
        else:
            fruitless += 1

    boundary = _when(fresh[-1].created) if fresh else forgiveness
    if boundary is not None:
        fruitless += _after(spent, boundary, observation.key, warnings)
    return fruitless >= stall_after


def _after(spent: list[str], instant: datetime, key: str, warnings: list[str]) -> int:
    """How many of a task's finished attempts began after some instant.

    Which is how many of them landed no log, whenever `instant` comes from the newest log there is: an attempt that landed one would be newer than the log it is being compared against.
    """
    count = 0
    for started in spent:
        if (when := _when(started)) is None:
            warnings.append(
                f"a recorded attempt of {key} has an unreadable start time "
                f"({started!r}) and is not counted toward the stall guard"
            )
        elif when > instant:
            count += 1
    return count


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


# the one reason whose log must not be handed back to the worker
RESET = frozenset({IncompleteReason.REDIRECTED})


def _spawn(observation: TaskObservation, inflight: InFlight) -> SpawnTask:
    """One task's spawn decision.

    A task with a prior log resumes it whatever went wrong — short, errored, cancelled, invalidated, a reshuffled slice, or a worker that died mid-run. The reason is reporting material rather than an input, because resume reuses exactly the samples still worth keeping: inspect looks each sample of the *new* slice up in the prior log by id, so a raised limit keeps its first ten and a moved range finds nothing to keep and runs the lot.

    **`REDIRECTED` is the exception, and it is the exception precisely because resume works.** A changed sandbox or gateway leaves the sample set identical, so every sample would be found, every answer reused, and the worker would finish having run nothing — a task reported as re-run and byte-identical to the one that was stale. Nothing about the *slice* changed, so nothing about the lookup fails; what changed is that the answers are no longer worth having, which is a judgement only the reason carries. So a redirected task is spawned without its prior log, and that log stays where it is as a superseded attempt.
    """
    current = observation.current
    task = observation.task
    resume = None if observation.reason in RESET else current
    return SpawnTask(
        identifier=observation.identifier,
        key=observation.key,
        resume=resume.location if resume is not None else None,
        attempt=_attempt(observation, inflight),
        reason=observation.reason,
        # from the manifest, never recomputed. These are what let a worker skip
        # constructing tasks it was not given, and they only work if they equal
        # what capture recorded -- a value derived here would be a second
        # opinion about the same question, and a disagreement would silently
        # prune the task it was meant to spare
        registry_name=task.registry_name if task is not None else None,
        args_hash=task.args_hash if task is not None else None,
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
    """Sample concurrency for a worker: five sources, most specific first.

    | | |
    |---|---|
    | the **shell's** `Pool.max_samples` | `STEWARD_MAX_SAMPLES` or `INSPECT_EVAL_MAX_SAMPLES` in the environment this turn ran in, so nothing outranks it |
    | the **run's** override | what `steward launch` resolved, carried in the manifest so a 02:00 tend reads the same number the launch did |
    | the **definition's** `max_samples` | how many samples a task should run at once is a property of the eval, and its author knows the workload |
    | the **ramp's floor** | a range was written, or the default one applies — the floor is where every task starts |
    | `DEFAULT_MAX_SAMPLES` | ramping was switched off and nobody said what to pin instead |

    The distinction between the first two is why `Pool.max_samples` is optional rather than pre-filled with the default. Collapsing them gets it wrong in one direction or the other — Steward's own fallback silently outranking a definition, or an explicit operator instruction silently losing to one — and neither is visible from the resulting number.

    Whichever wins is written into the selection explicitly. Under the first two rows it is a setpoint; under the last two it is a starting point, and the tuning loop changes where a worker ends up rather than where it begins. Which regime applies is `resolve_samples_ramp`'s answer, and the two functions agree by construction: a ramp exists exactly when neither of the pinning rows fired.

    Args:
        manifest: Captured desired state.
        pool: What the operator asked for.

    Returns:
        Sample concurrency for every worker this reconcile spawns.
    """
    if pool.max_samples is not None:
        return pool.max_samples
    if manifest.overrides is not None and manifest.overrides.max_samples is not None:
        return manifest.overrides.max_samples

    # options is free-form and deserialized, so a manifest written by another
    # version can carry anything here; a definition that set nothing carries None
    requested: Any = manifest.options.get("max_samples")
    if isinstance(requested, int) and requested > 0:
        return requested

    ramp = resolve_samples_ramp(manifest, pool)
    return ramp[0] if ramp is not None else DEFAULT_MAX_SAMPLES


def resolve_max_tasks(manifest: Manifest, pool: Pool) -> int | None:
    """Fleet width: how many tasks may be in flight at once, or `None` for all of them.

    The `max_samples` chain, one key over — this shell's `STEWARD_MAX_TASKS`, then the run's own override, then the definition, then unbounded — and it lives here for the same reason: `_steward.yaml` is not a source, so the file has nothing to say and `resolve_pool` has no manifest to ask.

    **The run's override is in the manifest and the definition's value is in `options`, and the two are deliberately different fields.** `options` records what the definition asked for, so that a runner can see what it is displacing; `overrides` records what this run replaced it with. Reading only the first would silently ignore a `steward launch --max-tasks`, and reading only the second would lose the definition's value the moment nobody overrode it.

    **`max_tasks` is inspect's word, so the definition owns it.** An earlier version made this a `_steward.yaml` key, justified by the reaches-the-runtime test: a definition's value never survives to a worker, because the selection document overrides it unconditionally with that worker's own batch size, so the file contradicted nothing. The test was sound and the key was still confusing — `eval_set()` knows the word, and somebody writing it there watched it do nothing while a same-named key lived in the policy file. The simpler rule is worth the migration: **inspect's words go in the definition; `_steward.yaml` holds only words `eval_set()` does not know** (execution.md, item 17).

    One divergence from `eval_set()` worth stating rather than hiding: unset means *everything at once* here, where `eval()`'s own rule is one task at a time for a single model. A fleet exists to run wide, and a definition that says nothing has expressed no preference rather than a preference for sequential.

    **The resolved number is used at reconcile and nowhere else.** It bounds how many batches are in flight; it is never written into a worker, which is told only its own share.

    Args:
        manifest: Captured desired state.
        pool: What the operator asked for.

    Returns:
        Tasks in flight allowed, or `None` for unbounded.
    """
    if pool.max_tasks is not None:
        return pool.max_tasks
    if manifest.overrides is not None and manifest.overrides.max_tasks is not None:
        return manifest.overrides.max_tasks

    # options is free-form and deserialized, so a manifest written by another
    # version can carry anything here; a definition that set nothing carries None
    requested: Any = manifest.options.get("max_tasks")
    return requested if isinstance(requested, int) and requested > 0 else None


def resolve_samples_ramp(manifest: Manifest, pool: Pool) -> tuple[int, int] | None:
    """The range the tuning loop may explore, or `None` where the setpoint is pinned.

    An explicit `max_samples` anywhere — this shell, the run's own override, or the definition — pins the value and switches the policy off entirely, which is what lets `samples_ramp` live in `_steward.yaml` without ever contradicting a definition: the key governs only Steward's own exploration, and the moment anybody expresses a setpoint there is nothing left for it to govern. An author who wants a custom start *and* a ramp writes the start as the range's floor.

    When pinned, the signal still runs — a persistently clean, saturated window against a pinned setpoint becomes a `tuning_proposal` item rather than a move, because the pin is somebody's and only they may move it (`_tend.tuning`).

    Args:
        manifest: Captured desired state.
        pool: What the operator asked for.

    Returns:
        The (floor, ceiling) to explore, or `None` where nothing may be moved.
    """
    if pool.max_samples is not None:
        return None
    if manifest.overrides is not None and manifest.overrides.max_samples is not None:
        return None
    requested: Any = manifest.options.get("max_samples")
    if isinstance(requested, int) and requested > 0:
        return None
    if pool.samples_ramp is False:
        return None
    if isinstance(pool.samples_ramp, tuple):
        return pool.samples_ramp
    return DEFAULT_SAMPLES_RAMP


def _spawn_level(
    batch: tuple[SpawnTask, ...],
    start: int,
    levels: Mapping[str, int],
    ramp: tuple[int, int] | None,
) -> int:
    """Where this worker's sample concurrency begins.

    The resolved start, or the level the tuning loop already climbed its tasks to — a respawn picks up where the climb left off rather than re-earning it twenty samples at a time. The minimum over a packed batch, because a selection carries one value applied per task: a fresh task must not inherit a sibling's climb, and an under-started climbed task costs one tend before the loop re-raises it, where the other direction would overshoot a level nothing measured.

    **A recorded level is clamped into the range in force now**, because the range can be edited between the climb and the respawn. A run that reached 200 under `[40, 300]` and is then narrowed to `[40, 100]` must come back at 100: the journal says what was authorized then, and `_steward.yaml` says what is authorized now, and a spawn answers to the second. Only the replay is clamped — `start` is already the resolved floor.
    """
    recorded = [levels[task.identifier] for task in batch if task.identifier in levels]
    if ramp is not None:
        floor, ceiling = ramp
        recorded = [min(max(level, floor), ceiling) for level in recorded]
    if len(recorded) < len(batch):
        recorded.append(start)
    return max(min(recorded), 1)


def _summarize(
    observed: ObservedTasks,
    *,
    running: set[str],
    workers: int,
    spawning: list[SpawnWorker],
    queued: int,
    blocked: "Blocked | None",
    capture_rss: int | None,
    stalled: list[str],
    archiving: int,
    pool: Pool,
    max_tasks: int | None,
    paused: bool,
    rerunning: int = 0,
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
        max_tasks=max_tasks,
        blocked=blocked,
        capture_rss=capture_rss,
        paused=paused,
        rerunning=rerunning,
    )
