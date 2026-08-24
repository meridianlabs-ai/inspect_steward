"""The decision. Given what should be running and what is, what to do next.

Everything before this reads; this is the step that decides. `reconcile` takes desired state (the manifest), what is running (the in-flight record, already resolved to live and departed), and what has happened (a log directory observation), and returns the actions that close the gap.

It is **pure** — no clock, no filesystem, no processes — and the design leans on that in three places (execution.md, *The reconcile core, and its drivers*):

- **Testability.** Scheduling correctness becomes "given this state, what actions?", which is a table.
- **Crash recovery is the ordinary path.** There is no resume routine to get wrong: recovery is just the next call, exercised on every tend.
- **`status` is `tend --dry-run`.** The same call with the actions discarded rather than executed — which only holds if computing them has no side effects at all.

What this function decides is mechanical continuity: which workers to spawn and in what order, which departed workers need recording. What a run *means* — whether an error class is systemic, whether an arm is worth continuing — is not here and is not Steward's (execution.md, *What the supervisor decides, and what it escalates*).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspect_ai._eval.evalset import TASK_IDENTIFIER_VERSION

from .._evalset.manifest import Manifest
from .._evalset.observe import (
    IncompleteReason,
    ObservedTasks,
    TaskObservation,
    TaskState,
)

DEFAULT_MAX_WORKERS = 10
"""Starting ceiling on concurrent workers.

Deliberately **not** derived from core count. A worker is on the CPU in bursts — transcript construction, serialization, compression, scoring — and waiting on a model API in between, so ten workers on four cores is ordinary rather than oversubscribed. What one process per task buys is isolation of those bursts, not core saturation, which makes the ceiling a resource guard rather than a parallelism budget: a number to tune, not a formula to derive (scheduling.md, *Launch everything, up to a ceiling*).

Ten is where `eval_set()`'s own `max_tasks` default starts, and it is expected to be raised.
"""

DEFAULT_MAX_SAMPLES = 40
"""Starting sample concurrency per worker.

Deliberately modest, because the ratchet is asymmetric: raising a limit takes effect immediately, lowering one only stops new acquires and waits for in-flight samples to drain. Climbing from a low setpoint is cheap; descending from a high one is not (scheduling.md, *`max_samples` — set explicitly, so it can be steered*).
"""


class ManifestVersionError(Exception):
    """A manifest whose identifiers cannot be matched against the running inspect.

    Raised rather than reported, because the two inputs are not comparable and every derived number would be wrong in the same direction: unmatchable identifiers make every task read *missing* and every log read *orphaned*, so a finished sweep reads as one that never started. A summary carrying that would look entirely normal, and a caller that forgot to check a flag would re-run a night's compute. An exception cannot be forgotten.
    """


@dataclass(frozen=True)
class Pool:
    """What the operator asked of the worker pool.

    Its two knobs are one budget spent twice — `workers × max_samples` is the fleet's total concurrent samples — and Steward owns both factors, which is what makes its load on a provider deterministic rather than emergent (scheduling.md, *Total concurrency is one budget spent twice*).
    """

    max_workers: int = DEFAULT_MAX_WORKERS
    """Ceiling on concurrent workers. Steward's alone: a definition has nothing to say about it, since worker mode runs one task per process and `max_tasks` is moot."""

    max_samples: int | None = None
    """Sample concurrency per worker, or `None` for no operator preference.

    `None` rather than the default itself, because the two are not the same claim: *no preference* yields to whatever the definition asked for, and a number is an instruction that does not. See `resolve_max_samples`.
    """


@dataclass(frozen=True)
class RunningWorker:
    """A worker confirmed alive.

    The resolved view, not the record: deciding whether a recorded worker is still alive means reading the process table and the control discovery directory, which is I/O and belongs to whatever produces this.
    """

    worker: str
    """Worker stem — the name of its selection document and its output file, and what the record keys on."""

    identifier: str
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
    identifier: str
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

    @property
    def running_identifiers(self) -> set[str]:
        return {worker.identifier for worker in self.running}


@dataclass(frozen=True)
class SpawnWorker:
    """Run this task.

    Everything a selection document needs, decided but not yet written.
    """

    identifier: str

    key: str
    """Display key from the manifest."""

    resume: str | None
    """Location of a prior log to resume, or `None` to start fresh. Completed, non-errored, non-invalidated samples are reused, so a resume of a five-hundred-sample task with forty-seven errors runs forty-seven samples."""

    max_samples: int

    attempt: int
    """1-based, counting the logs already in the directory."""

    reason: IncompleteReason | None
    """Why more work is needed (`None` when the task has never run)."""


@dataclass(frozen=True)
class ReapWorker:
    """Record that a worker is gone."""

    worker: DepartedWorker


Action = SpawnWorker | ReapWorker


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
    spawning: int
    queued: int

    orphans: list[str]
    """Identifiers in the log directory that the manifest does not name. Reported rather than acted on: archiving them is gated on explicit acceptance, because a one-character change to a task arg reads identically to a deliberate removal (workflow.md, *One trigger, and one gate on it*)."""

    orphans_running: list[str]
    """Orphaned identifiers that still have a live worker. Stopping them belongs with the same gate."""

    unreadable: int
    """Files in the log directory that could not be read as logs."""

    max_workers: int
    paused: bool


@dataclass(frozen=True)
class Reconciliation:
    """What to do, what is waiting, and where things stand."""

    actions: list[Action]
    """To execute in order. A `status` computes these and throws them away."""

    queued: list[SpawnWorker]
    """Would spawn, but the pool is full. The same decision deferred, which is what lets an authorized re-run jump the queue by sorting rather than by a second code path (scheduling.md, *Approved re-runs go first*)."""

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
        pool: Worker ceiling and default sample concurrency.
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

    pending = [
        _spawn(observation, max_samples)
        for observation in _spawn_order(observed.tasks)
        if observation.identifier not in running
    ]

    slots = (
        0
        if paused
        else max(0, min(pool.max_workers - len(inflight.running), len(pending)))
    )
    spawning, queued = pending[:slots], pending[slots:]

    actions: list[Action] = [ReapWorker(worker) for worker in inflight.departed]
    actions.extend(spawning)

    return Reconciliation(
        actions=actions,
        queued=queued,
        summary=_summarize(
            observed,
            running=running,
            spawning=len(spawning),
            queued=len(queued),
            pool=pool,
            paused=paused,
        ),
    )


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


def _spawn(observation: TaskObservation, max_samples: int) -> SpawnWorker:
    """One task's spawn decision.

    A task with a prior log resumes it whatever went wrong — short, errored, cancelled, invalidated, or a worker that died mid-run. There is deliberately no branch on the reason: resume reuses exactly the samples that are worth keeping, so the reason is reporting material rather than an input to the decision.
    """
    current = observation.current
    return SpawnWorker(
        identifier=observation.identifier,
        key=observation.key,
        resume=current.location if current is not None else None,
        max_samples=max_samples,
        attempt=len(observation.superseded) + (1 if current is not None else 0) + 1,
        reason=observation.reason,
    )


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
    spawning: int,
    queued: int,
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
        spawning=spawning,
        queued=queued,
        orphans=orphans,
        orphans_running=[identifier for identifier in orphans if identifier in running],
        unreadable=len(observed.unreadable),
        max_workers=pool.max_workers,
        paused=paused,
    )
