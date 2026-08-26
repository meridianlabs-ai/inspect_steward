"""One row per task: how far it has got, and how hard it is working.

The counts a turn already computes answer *what is Steward doing* — two to spawn, one to archive. They do not answer the question anybody actually opens a status view to ask, which is **how is the run going**, and that question is about samples rather than tasks. A sweep of four tasks is four rows either way; the difference is whether a row says `incomplete` or `37/502  20%  83r  63q`.

**Every column comes from something already being read.** Sample counts, the headline metric, and the per-sample budgets are in the log header, which observation reads anyway and the cache now mostly skips. Live counts, in-flight totals, and the model connection pool come from the running worker's own socket, which is only consulted when something is actually running. A finished campaign costs one directory listing.

**Settled and live rows are the same shape, filled from different places.** A task whose worker has exited is described entirely by its log; one still running is described by its log for the denominators and by its process for everything that moves. Both produce a `TaskProgress`, so the renderer has no branch and the two cannot drift apart in what they report.
"""

from dataclasses import dataclass, field

from .._evalset.display import KeyParts, ShortKeys, shorten_keys
from .._evalset.observe import (
    LIMITS,
    LogAttempt,
    ObservedTasks,
    TaskObservation,
    TaskState,
)
from .._worker import LiveFleet, LiveTask

SUFFIX = {
    "turns": "t",
    "messages": "m",
    "tokens": "k",
    "time": "s",
    "working": "w",
    "cost": "$",
}
"""One character per budget, so a limit column says which limit it is showing without a header."""


@dataclass(frozen=True)
class Budget:
    """A per-sample limit and how close the leading sample is to it."""

    name: str
    used: int
    limit: int

    @property
    def suffix(self) -> str:
        return SUFFIX.get(self.name, "?")

    @property
    def fraction(self) -> float:
        return self.used / self.limit if self.limit else 0.0


@dataclass(frozen=True)
class TaskProgress:
    """One task's row."""

    key: str
    """Display key — name, args, and model, as the manifest computed it. Unique across the run, and usually longer than a reader needs; `short_keys` shortens it against the rows actually on screen."""

    name: str | None = None
    solver: str | None = None
    model: str | None = None
    """The key still in pieces, so shortening does not have to parse back out of a string it built. `None` for an orphan, which has no manifest row to take them from."""

    state: TaskState = TaskState.MISSING
    identifier: str = ""

    completed: int = 0
    """Samples finished without error."""

    total: int = 0
    """Samples the task comprises."""

    errored: int = 0
    running: int = 0
    queued: int = 0

    headline: float | None = None
    headline_name: str | None = None
    """The metric in the score column, and which metric it is. Chosen by convention — see `LogAttempt.headline`."""

    budget: Budget | None = None
    """The per-sample limit worth showing, and its usage. `None` when the task declared none, or when nothing is running to have used any."""

    connections: tuple[int, int | None] | None = None
    """Model connections in use and the pool's ceiling, for a running task."""

    live: bool = False
    """Whether a worker answered for this row. When false the numbers are the log's last word rather than the present tense."""

    unavailable: str | None = None
    """Why a running worker could not be read — `busy`, `gone`. The row still renders from its log."""

    @property
    def fraction(self) -> float:
        return self.completed / self.total if self.total else 0.0


def task_progress(observed: ObservedTasks, fleet: LiveFleet) -> list[TaskProgress]:
    """One row per task, in manifest order then orphans.

    Args:
        observed: The log directory read against the manifest.
        fleet: What the running workers reported, empty when none are.

    Returns:
        A row per observed task.
    """
    return [_row(task, fleet.tasks.get(task.identifier)) for task in observed.tasks]


def _row(task: TaskObservation, live: LiveTask | None) -> TaskProgress:
    attempt = task.current
    answered = live is not None and live.unavailable is None

    completed, total, errored = _counts(task, attempt, live if answered else None)
    return TaskProgress(
        key=task.key,
        # `display_name or name`, exactly as `compute_display_keys` builds the
        # full key from: a task the manifest calls `Friendly Name` must not
        # shorten to the internal name nothing else in the run ever shows
        name=(task.task.display_name or task.task.name) if task.task else None,
        solver=task.task.solver if task.task is not None else None,
        model=task.task.model if task.task is not None else None,
        state=task.state,
        identifier=task.identifier,
        completed=completed,
        total=total,
        errored=errored,
        running=live.samples.in_flight if answered and live is not None else 0,
        queued=live.samples.queued if answered and live is not None else 0,
        headline=attempt.headline if attempt is not None else None,
        headline_name=attempt.headline_name if attempt is not None else None,
        budget=_budget(attempt, live if answered else None),
        connections=(
            (live.connections.in_use, live.connections.limit)
            if answered and live is not None
            else None
        ),
        live=answered,
        unavailable=live.unavailable if live is not None else None,
    )


def _counts(
    task: TaskObservation, attempt: LogAttempt | None, live: LiveTask | None
) -> tuple[int, int, int]:
    """Completed, total, and errored samples — from the worker when there is one.

    A running worker's own count is ahead of its log, which is buffered, so it wins wherever it exists. The manifest's `required_samples` is the denominator of last resort, because a task that has never run has no log to ask and its total is still known.
    """
    # an orphan has no manifest row and so no required count -- its log is the
    # only thing that knows how big it was
    required = task.required_samples or 0
    if live is not None and live.samples.total:
        return live.samples.completed, live.samples.total, live.samples.errored
    if attempt is not None:
        return (
            attempt.completed_samples,
            attempt.total_samples or required,
            attempt.errored_samples,
        )
    return 0, required, 0


def _budget(attempt: LogAttempt | None, live: LiveTask | None) -> Budget | None:
    """The limit worth showing, of however many the task declared.

    **The one closest to being reached**, because that is the one that will stop a sample, and a task declaring both a turn limit and a token limit is not asking a reader to pick.

    **Only while something is running.** Usage comes from the worker, so a settled task has a limit and no number to put against it — and rendering that as `0/30` would say *used none of thirty*, which is a claim rather than a gap. A finished task's budget is not interesting anyway: whatever it spent, it finished.
    """
    if attempt is None or not attempt.limits or live is None:
        return None

    used = {
        "turns": live.usage.turns,
        "messages": live.usage.messages,
        "tokens": live.usage.tokens,
        "time": int(live.usage.seconds),
    }
    budgets = [
        Budget(name=name, used=used.get(name, 0), limit=limit)
        for name in LIMITS
        if (limit := attempt.limits.get(name)) is not None
    ]
    if not budgets:
        return None
    return max(budgets, key=lambda budget: budget.fraction)


@dataclass(frozen=True)
class Progress:
    """The whole run, as rows plus the totals a header line wants."""

    rows: list[TaskProgress] = field(default_factory=list[TaskProgress])

    @property
    def completed(self) -> int:
        return sum(row.completed for row in self.rows)

    @property
    def total(self) -> int:
        return sum(row.total for row in self.rows)

    @property
    def running(self) -> int:
        return sum(row.running for row in self.rows)

    @property
    def queued(self) -> int:
        return sum(row.queued for row in self.rows)

    @property
    def errored(self) -> int:
        return sum(row.errored for row in self.rows)

    @property
    def fraction(self) -> float:
        return self.completed / self.total if self.total else 0.0


def short_keys(rows: list[TaskProgress]) -> ShortKeys:
    """The display keys for these rows, no longer than they have to be.

    One helper rather than two, because the terminal and the markdown table must not disagree about what a row is called.

    Args:
        rows: The rows about to be rendered, in render order.

    Returns:
        Keys index-aligned with `rows`, and the model they all share if none of them shows it.
    """
    return shorten_keys(
        [
            KeyParts(
                name=row.name if row.name is not None else row.key,
                solver=row.solver,
                model=row.model,
                full=row.key,
            )
            for row in rows
        ]
    )
