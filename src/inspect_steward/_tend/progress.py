"""One row per task: how far it has got, and how hard it is working.

The counts a turn already computes answer *what is Steward doing* — two to spawn, one to archive. They do not answer the question anybody actually opens a status view to ask, which is **how is the run going**, and that question is about samples rather than tasks. A sweep of four tasks is four rows either way; the difference is whether a row says `incomplete` or `37/502  20%  83r  63q`.

**Every column comes from something already being read.** Sample counts, the headline metric, and the per-sample budgets are in the log header, which observation reads anyway and the cache now mostly skips. Live counts, in-flight totals, and the model connection pool come from the running worker's own socket, which is only consulted when something is actually running. A finished campaign costs one directory listing.

**Settled and live rows are the same shape, filled from different places.** A task whose worker has exited is described entirely by its log; one still running is described by its log for the denominators and by its process for everything that moves. Both produce a `TaskProgress`, so the renderer has no branch and the two cannot drift apart in what they report.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from .._evalset.display import KeyParts, ShortKeys, shorten_keys
from .._evalset.observe import (
    LogAttempt,
    ObservedTasks,
    TaskObservation,
    TaskState,
)
from .._util.size import format_bytes
from .._worker import LiveFleet, LiveParked, LiveTask, ProcessUsage, process_usage

SUFFIX = {
    "turns": "t",
    "messages": "m",
    "tokens": "tk",
    "time": "s",
    "working": "w",
    "cost": "$",
}
"""What a budget is labelled with, so a limit column says which limit it is showing without a header.

`tk` rather than `k` for tokens, because the counts are abbreviated and `10Mk` reads as a unit prefix rather than as a noun.
"""

ORDER = ("turns", "tokens", "messages", "cost", "working", "time")
"""Which budget a row shows when a task declared several, most telling first.

**One column, not one per limit.** A task declaring a turn limit and a token limit is not asking a reader to pick between two numbers, and a table wide enough for six of them is a table nobody reads on a phone.

The order is by how directly the budget describes the *work*: turns and tokens are what an agent spends, messages is a proxy for turns, and the three at the end are wall-clock and money — real ceilings, but ones that say more about the machine than about the task. Ordered rather than chosen by whichever is closest to tripping, because a column that changes which limit it reports between one turn and the next is not a column a reader can follow.
"""


@dataclass(frozen=True)
class Budget:
    """A per-sample limit and how far the typical running sample has got against it."""

    name: str
    used: int
    limit: int

    @property
    def suffix(self) -> str:
        return SUFFIX.get(self.name, "?")

    @property
    def fraction(self) -> float:
        return self.used / self.limit if self.limit else 0.0

    @property
    def text(self) -> str:
        """The cell, as a table renders it — `122/200t`, `1.1M/10Mtk`.

        Flush against the number, like `83r` and `63q` in the columns to its left. The whole cell is one quantity and the space made it read as two.
        """
        if self.name in ("time", "working"):
            return f"{self.used}/{self.limit}{self.suffix}"
        return f"{compact(self.used)}/{compact(self.limit)}{self.suffix}"


def compact(value: int) -> str:
    """A count short enough for a column: `122`, `48k`, `1.1M`.

    Token budgets run to seven and eight digits, and a cell reading `1143820/10000000` costs more width than the rest of the row together while being harder to compare against the limit beside it than `1.1M/10M` is.
    """
    for unit, scale in (("M", 1_000_000), ("k", 1_000)):
        if value >= scale:
            scaled = value / scale
            return f"{scaled:.1f}{unit}" if scaled < 10 else f"{round(scaled)}{unit}"
    return str(value)


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
    """Samples not started yet — the worker's own count where one is answering, and what the counts leave over where none is."""

    headline: float | None = None
    headline_name: str | None = None
    """The metric in the score column, and which metric it is. Declared by the task where it says — see `LogAttempt.headline`."""

    budget: Budget | None = None
    """The per-sample limit worth showing, and how far into it a typical running sample is. `None` when the task declared no limit anything can be measured against, or when nothing is running to have spent any."""

    connections: tuple[int, int | None] | None = None
    """Model connections in use and the pool's ceiling, for a running task."""

    parked: LiveParked = field(default_factory=LiveParked)
    """Samples of this task waiting on a person. Empty for anything not running.

    Not a column — a park is a *decision somebody owes*, so it becomes an item rather than a number in a table, and the row is only how it gets here. Carried per task rather than per worker because that is what a person recognises: a packed worker holding two parked tasks is two decisions.
    """

    pid: int = 0
    """The worker running this task, or `0` where none is.

    Here for the park and nothing else: an ACP socket is discovered by pid, so this is what turns *somebody is waiting* into an address they can be answered at.
    """

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


@dataclass(frozen=True)
class Live:
    """What only a running process can say, and nothing writes down.

    A block rather than more columns, because none of it is per task in the way the table is: refusals and retries are per task but meaningless once one finishes, and memory and CPU are per *process*, which a packed worker shares between tasks. Summing either down a column would answer a question nobody asked.

    Everything here is **live-only** and every rendering of it says so. Inspect records neither refusals nor HTTP retries in an eval log, so a total built from them describes the tasks running at this instant — and one that *falls* as tasks complete reads as a problem resolving itself when it is only work finishing (agent.md §4.2).
    """

    tasks: int
    """Running tasks whose worker answered, which is the denominator for the tallies below and for nothing else."""

    refusals: int
    http_retries: int

    unavailable: int = 0
    """Running tasks whose worker did not answer.

    Counted and named rather than dropped, for the reason every other omission in this summary is: `0 refusals` over a fleet where nothing answered is a claim about the run, and the reading that produced it made no such claim.
    """

    usage: ProcessUsage = field(default_factory=ProcessUsage)
    """What the live processes are costing the machine, counted once per pid.

    **Measured over every worker Steward believes is alive, not only the ones that answered.** A busy worker is exactly the one whose memory is worth knowing, and its resident set comes from the kernel rather than from the socket it is too busy to serve — so its silence costs the tallies above and must not cost this. It also covers a worker that has not bound a control socket yet, which is the fleet's first seconds, when the figure is climbing fastest.
    """

    @property
    def figures(self) -> str:
        """The block as one line, which both renderings put their own label in front of.

        Here rather than in either renderer, for the reason the item type exists: two hand-written versions of the same figures are two chances to disagree, and the summary is required to say the same thing in a terminal and in a document.
        """
        parts: list[str] = []
        if self.tasks:
            parts += [
                f"{self.tasks} task{'' if self.tasks == 1 else 's'}",
                f"{self.refusals} refusals",
                f"{self.http_retries} HTTP retries",
            ]
        if self.unavailable:
            parts.append(f"{self.unavailable} not answering")
        if self.usage.processes:
            processes = self.usage.processes
            parts.append(
                f"{format_bytes(self.usage.rss)} across {processes} "
                f"process{'' if processes == 1 else 'es'}"
            )
            parts.append(f"{self.usage.cores:.1f} cores, average since start")
        return " · ".join(parts)


LIVE_ONLY = (
    "Live only: refusals and HTTP retries are recorded in no eval log, so these "
    "describe the tasks running right now and fall as tasks finish."
)
"""The caveat that has to travel with the block, in one place so both renderings carry the same one.

Not a nicety. Every figure in the block is about *what is running*, so all of them shrink as a run completes — and a reader watching refusals drop from forty to zero will read a problem resolving itself when what happened is that the work finished (agent.md §4.2).
"""


def live_totals(fleet: LiveFleet, pids: Iterable[int] = ()) -> Live | None:
    """The fleet's live block, or `None` where nothing is running.

    **Two populations, because the two kinds of figure fail independently.** A refusal count comes from a worker's control socket, so a worker that did not answer contributes *no* refusals rather than zero of them — and is counted as unavailable instead. Resident memory comes from the kernel, which answers for a busy worker exactly as readily as for an idle one, so usage is measured over every process Steward believes is alive.

    Conflating the two is what made a fleet of eight busy workers render as *nothing is running* and fall back to a startup projection while eight processes held real memory.

    Args:
        fleet: What the running workers reported.
        pids: Every worker Steward believes is alive, whether or not it answered. Deduplicated downstream, since a packed worker reports a row per task and all of them name one process.

    Returns:
        The block, or `None` when nothing is running to describe.
    """
    running = list(fleet.tasks.values())
    answered = [task for task in running if task.unavailable is None]
    usage = process_usage([*pids, *(task.pid for task in answered)])
    if not answered and not usage.processes:
        return None
    return Live(
        tasks=len(answered),
        refusals=sum(task.refusals for task in answered),
        http_retries=sum(task.http_retries for task in answered),
        unavailable=len(running) - len(answered),
        usage=usage,
    )


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
        queued=_queued(task, live if answered else None, completed, total, errored),
        headline=attempt.headline if attempt is not None else None,
        headline_name=attempt.headline_name if attempt is not None else None,
        budget=_budget(attempt, live if answered else None),
        connections=(
            (live.connections.in_use, live.connections.limit)
            if answered and live is not None
            else None
        ),
        parked=live.parked if answered and live is not None else LiveParked(),
        pid=live.pid if live is not None else 0,
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


def _queued(
    task: TaskObservation,
    live: LiveTask | None,
    completed: int,
    total: int,
    errored: int,
) -> int:
    """Samples this task has not started yet.

    **Derived where no worker is answering, rather than left at zero.** A worker reports its own queue and is the better source while there is one, but most of a sweep is not running at any moment — and a table where the queue column appears only for the tasks that happen to have a live worker is a column that comes and goes between one post and the next. What a reader wants from it is the same either way: how much of this is still to come.

    A task nothing will run has no queue. That is a finished task, and an orphan, which has no manifest row asking for it.
    """
    if live is not None:
        return live.samples.queued
    if task.state in (TaskState.COMPLETE, TaskState.ORPHANED):
        return 0
    return max(0, total - completed - errored)


def _budget(attempt: LogAttempt | None, live: LiveTask | None) -> Budget | None:
    """The limit worth showing, of however many the task declared.

    **The first of `ORDER` the task both declared and can be measured against**, which is one column whichever combination it declared.

    **From the header and the worker together.** Turn, message and time ceilings are in the log header, which is where they were launched from and where they stay. The token ceiling comes from the worker where a worker is answering, because that one moves: `inspect ctl config` retunes it mid-run, and a formula limit's ceiling is only meaningful beside the metering rule the worker applies. Cost and working time are declared in the header and reported by nothing, so a task with only those shows no column rather than a ceiling with no progress against it.

    **Only while something is running.** Usage comes from the worker, so a settled task has a limit and no number to put against it — and rendering that as `0/30` would say *used none of thirty*, which is a claim rather than a gap. A finished task's budget is not interesting anyway: whatever it spent, it finished.
    """
    if live is None:
        return None

    limits = dict(attempt.limits) if attempt is not None else {}
    if live.usage.token_limit is not None:
        limits["tokens"] = live.usage.token_limit
    used = {
        "turns": live.usage.turns,
        "messages": live.usage.messages,
        "tokens": live.usage.tokens,
        "time": int(live.usage.seconds),
    }
    for name in ORDER:
        if name in used and (limit := limits.get(name)) is not None:
            return Budget(name=name, used=used[name], limit=limit)
    return None


@dataclass(frozen=True)
class Progress:
    """The whole run, as rows plus the totals a header line wants."""

    rows: list[TaskProgress] = field(default_factory=list[TaskProgress])

    live: Live | None = None
    """The live block, or `None` where nothing is running to have one.

    Absent rather than zeroed, and the distinction is the whole reason for the type: `0 refusals` about a finished campaign is a claim about the run, and there is nothing here that could support it. What takes its place is the capture's startup bound — a ceiling is the useful figure before there is an actual, and the actual is the useful one once there is.
    """

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
