"""Reading a log directory as structured state.

Steward converges a log directory toward a manifest. This module is the read half of that: a directory of eval logs becomes an observation, and an observation plus a manifest becomes a per-task verdict — complete, incomplete, missing, or orphaned. `reconcile` turns those verdicts into actions; nothing here decides anything.

The work splits at the filesystem boundary rather than at the manifest:

- `observe_logs` does the I/O. It groups attempts by `task_identifier` and knows nothing about what was *supposed* to run — which is what lets it serve `logs-archive/` and any other directory where there is no manifest to compare against (workflow.md, *Steward never destroys a result, but it does curate the directory*).
- `observe_tasks` is pure. It answers completeness, which needs the manifest's per-task sample and epoch counts, and it names identifiers present in the directory but absent from the definition.

Two properties are borrowed from the journal, because they are properties of anything Steward reads on a schedule and cannot afford to choke on:

- **An unreadable log costs one log, never the directory.** A zip that exists but has not yet been written far enough to have a header is an ordinary transient during worker startup, and a tend that raised on it would be a tend that never ran. Damage is reported with a reason, and the caller decides whether to complain.
- **Only headers are ever read here.** Anomaly classification (`instances.py`) goes below the header — summaries and single samples, at costs its own docstring accounts for — and that module is now where the read discipline is stated whole. What survives intact everywhere is the rule the old absolute was really for: never a transcript, never events, never a whole log (agent.md, *Never read a full eval log*).
"""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# task_identifier is the pairing mechanism eval_set() itself uses; it is
# versioned rather than public, which is why the manifest records its version
from inspect_ai._eval.evalset import task_identifier

# the same per-task narrowing `eval_run` applies before a log records what ran
from inspect_ai._eval.task.util import resolve_task_sample_ids
from inspect_ai.log import (
    EvalLog,
    EvalLogInfo,
    headline_metric,
    list_eval_logs_async,
    read_eval_log,
    read_eval_log_async,
)
from inspect_ai.util._sandbox.environment import resolve_sandbox_environment

from .manifest import SELECTION, Manifest, ManifestTask

if TYPE_CHECKING:
    # the cache reads `LogAttempt` from here, so the run-time import goes one way
    # only and the annotation below is a string
    from .cache import AttemptCache

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
    """When the file was last written, in **milliseconds** since the epoch, as `EvalLogInfo` reports it.

    For a finished log this is when something last *changed* it, and an operator invalidating samples in it is the only thing that does — which makes it the one record of when they acted. `None` when the filesystem does not report one.
    """

    headline: float | None = None
    """The one number worth putting in a table, **as the task declared it**.

    This used to be a Steward convention — the first metric of the first score — because nothing in a log marked a metric as primary and every reader rendering a task × model table invented its own rule. Inspect answers it now: a task declares `headline_metric`, scoring resolves it onto `EvalResults.headline`, and `inspect_ai.log.headline_metric` reads whichever of the two a given log carries. So Steward reads the declaration where there is one and gets the old convention where there is not, which is exactly the fallback the resolver applies. `headline_name` still says which metric it landed on, because a number in a column is not self-describing either way.
    """

    headline_name: str | None = None
    """Which metric `headline` is, as `<score>/<metric>`."""

    selection: dict[str, Any] = field(default_factory=dict[str, Any])
    """Which samples this log ran — `limit`, `sample_id` and `sample_shuffle`, from `eval.config`, whichever were set.

    **Which samples ran, where `total_samples` only says how many.** All three are identity-neutral, so a run that changes one produces logs the manifest still pairs with its tasks — and a `(0, 5)` limit changed to `(5, 10)`, or a reshuffle, keeps the count identical too. Carried so `_reshaped` can tell *these five samples* from *five samples*.

    A mapping rather than a tuple because this survives a round trip through the attempt cache, where a tuple would come back a list and compare unequal to the one just read.
    """

    sandbox: str | None = None
    """The sandbox this log ran under, as `type` or `type:config`, from `eval.sandbox`.

    **The sandbox as resolved, which is not always the sandbox as asked for.** `--sandbox docker` becomes `docker:compose.yaml` where the task directory holds one, so this compares cleanly against an override that named a config and only on its type against one that did not (`_redirected`).
    """

    model_base_url: str | None = None
    """The gateway this log's model calls went to, from `eval.model_base_url`."""

    limits: dict[str, int] = field(default_factory=dict[str, int])
    """The per-sample budgets this log ran under, from `eval.config` — `turns`, `messages`, `tokens`, `time`, `working`, `cost`, whichever were set.

    Carried because *usage* comes from the control channel and the *limit* does not: `inspect ctl config` reports retune overrides rather than the task's declared values, so the denominator of a `115/300t` column can only come from here.
    """

    error_traceback: str | None = None
    """The traceback of the error that halted the eval, from `header.error`.

    Beside `error` because the two answer different questions: the message is display, the traceback is identity — a task that finished `status="error"` classes on its exception's type and raising frame (`classify.task_error_class`) with no read beyond the header this record already came from.
    """

    @property
    def errored_samples(self) -> int:
        """Samples that ran and failed.

        Nonzero is normal rather than exceptional: worker mode forces `fail_on_error=False`, so a task finishes `success` carrying whatever residue of errored samples it accumulated (execution.md, *Recovery*).
        """
        return max(0, self.total_samples - self.completed_samples)


@dataclass(frozen=True)
class UnreadableLog:
    """A file the run's results depend on and that could not be read.

    A log, ordinarily. Also a scan directory's compacted rows, which are a second thing a signature covers and a second hole nobody can size: a parquet that will not read is indistinguishable from a scanner that flagged nothing, and *nothing was flagged* is exactly the answer a signoff must not be allowed to assume.
    """

    location: str
    reason: str

    what: str = "a log"
    """What the file was being read as, for the item's sentence. The only reason this is a field is that the sentence is read by an operator deciding whether to acknowledge it, and *could not be read as a log* over a `.parquet` sends them to look for the wrong thing."""


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

    @property
    def locations(self) -> list[str]:
        """Every log this observation covers, readable or not.

        The directory as it stood, which is what a cache of it has to be narrowed to — an entry for a log that has since been archived is an entry nothing will ever ask for again.
        """
        return [
            attempt.location
            for attempts in self.attempts.values()
            for attempt in attempts
        ] + [log.location for log in self.unreadable]

    def current(self, identifier: str) -> LogAttempt | None:
        """The attempt that counts for an identifier.

        The latest *successful* attempt wins, falling back to the newest attempt when none succeeded. Deliberately not upstream's rule, which takes the newest by file mtime whatever its status: mtime is rewritten by restoring a log from the archive, and a re-run that errored should not displace a good result (execution.md, *Multiple logs per task*).

        **Recency is the whole rule here, because nothing else is available.** A directory read without a manifest — `logs-archive/`, or an orphan's attempts — has no question to compare a log against. Where there *is* a manifest, `_current` asks the better one first and falls back to this.
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

    RESHAPED = "reshaped"
    """Succeeded, and covers a different slice of the dataset than the run is now asking for — a different limit, a different sample selection, a different shuffle.

    The one incompleteness that is not about the log falling short. `task_identifier` ignores `limit`, `sample_id` and `sample_shuffle` on purpose, so that raising a limit resumes rather than orphaning what has run; the cost is that *changing which samples* run is invisible to it, and a count check cannot see it either once the counts agree. Without this a re-launch that reshuffles a limited run reports nothing to do and signs off on the previous subset.

    **Resumes, and that is the point.** Only the *slice* moved; every sample the log holds was answered under the settings still in force, so the ones still wanted are still good and only the difference has to run. A raised limit is the ordinary case and re-running its first ten samples would be pure waste.
    """

    REDIRECTED = "redirected"
    """Succeeded, and answered under settings the run has since pointed somewhere else — a different sandbox, a different model gateway.

    **The one reason that must not resume.** Every other kind of incompleteness leaves the log's finished samples worth keeping, so a spawn hands the worker its prior log and inspect reuses them per sample id. Here the sample *set* is unchanged and every answer in it is stale, so resuming would find all of them, reuse all of them, and complete having run nothing — the task would report as re-run and be byte-identical. So a redirected task starts fresh, and its prior log stays as a superseded attempt.

    Checked before the reasons that resume, because it outranks them: a log that is both short and redirected has to reset, and letting `SHORT` answer first would resume it.
    """


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
    cache: "AttemptCache | None" = None,
) -> ObservedLogs:
    """Read a log directory into attempts grouped by task identifier.

    Reads headers only, and reads them concurrently — which matters for the `.eval` format Inspect writes by default, where a header read genuinely awaits on I/O, and matters most when `log_dir` is remote.

    Args:
        log_dir: Directory to read. A directory that does not exist is an empty observation.
        concurrency: Headers to read at once.
        cache: Attempts already known from a previous turn, consulted per file against the listing's modification time and size. Filled in as this turn reads, and narrowed to what the listing still names, so the caller can write it back. Omitted, every header is read.

    Returns:
        Attempts per identifier and one entry per file that could not be read.
    """
    log_dir = str(log_dir)

    # not recursive: a log directory is flat by design, and scan output lives
    # in a subdirectory of it (execution.md, *One flat directory*)
    infos = await list_eval_logs_async(log_dir, recursive=False)

    semaphore = asyncio.Semaphore(concurrency)

    async def read(info: EvalLogInfo) -> LogAttempt | UnreadableLog:
        if cache is not None and (known := cache.get(info)) is not None:
            return known
        async with semaphore:
            try:
                header = await read_eval_log_async(info, header_only=True)
            except Exception as ex:
                return UnreadableLog(
                    location=info.name, reason=f"{type(ex).__name__}: {ex}"
                )
        attempt = _attempt(info.name, info.mtime, header)
        if cache is not None:
            cache.put(info, attempt)
        return attempt

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
    cache: "AttemptCache | None" = None,
) -> ObservedLogs:
    """Read a log directory into attempts grouped by task identifier.

    Args:
        log_dir: Directory to read. A directory that does not exist is an empty observation.
        concurrency: Headers to read at once.
        cache: Attempts already known from a previous turn. See `observe_logs_async`.

    Returns:
        Attempts per identifier and one entry per file that could not be read.
    """
    return asyncio.run(
        observe_logs_async(log_dir, concurrency=concurrency, cache=cache)
    )


def observe_tasks(manifest: Manifest, logs: ObservedLogs) -> ObservedTasks:
    """Classify a manifest's tasks against a log directory.

    Pure: no clock, no filesystem, no processes.

    Args:
        manifest: Desired state, read from the definition.
        logs: The log directory, as `observe_logs` read it.

    Returns:
        One observation per manifest task, in manifest order, followed by one per identifier found in the directory that the manifest does not name.
    """
    tasks = [_observe_task(manifest, task, logs) for task in manifest.tasks]

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


def _observe_task(
    manifest: Manifest, task: ManifestTask, logs: ObservedLogs
) -> TaskObservation:
    required = task.samples * task.epochs
    current = _current(manifest, task, logs)
    if current is None:
        return TaskObservation(
            identifier=task.identifier,
            state=TaskState.MISSING,
            task=task,
            required_samples=required,
        )

    reason = incomplete_reason(manifest, task, current)
    return TaskObservation(
        identifier=task.identifier,
        state=TaskState.COMPLETE if reason is None else TaskState.INCOMPLETE,
        task=task,
        reason=reason,
        current=current,
        superseded=[
            attempt
            for attempt in logs.attempts.get(task.identifier, [])
            if attempt is not current
        ],
        required_samples=required,
    )


def _current(
    manifest: Manifest, task: ManifestTask, logs: ObservedLogs
) -> LogAttempt | None:
    """The attempt that answers for a task, once the question being asked is known.

    `ObservedLogs.current` cannot ask this — it serves directories with no manifest to compare against, so its rule is the newest successful attempt and nothing else. That rule holds until a run's shape moves and moves back: slice A, then B, then A again, where the newest success answers B and the one before it answers A. Taking the newest then re-runs work that is sitting in the directory, and for a redirect it re-runs it from nothing.

    So the shape picks which attempt is the run's answer, and everything else — short, errored, invalidated — decides what to do with the one it picked. Where no attempt matches, the manifest-free rule stands: whichever log is going to be resumed is better chosen by *newest successful* than by nothing.
    """
    return next(
        (
            attempt
            for attempt in logs.attempts.get(task.identifier, [])
            if attempt.status == "success" and answers_shape(manifest, task, attempt)
        ),
        logs.current(task.identifier),
    )


def incomplete_reason(
    manifest: Manifest, task: ManifestTask, attempt: LogAttempt
) -> IncompleteReason | None:
    """Why an attempt does not answer for a task, or `None` when it does.

    **The one definition of *complete*, so that nothing can hold a second one.** `observe_tasks` calls this to classify a task, and the log store's read calls it to decide whether a candidate is worth copying in — and those two answering differently is not a discrepancy, it is a launch reporting *this work does not run here* about a task the very next tend queues. A store filter written from the same ingredients but not the same code drifted exactly that way: it compared `completed_samples` where this compares `total_samples`, so a signed log carrying samples somebody accepted as errored was published by one and refused by the other.

    **A redirect answers first**, because it invalidates every sample in the log where every other reason leaves them worth resuming, so one of those must not mask it.

    Args:
        manifest: Desired state, whose overrides and options say what is being asked for.
        task: The manifest row this attempt is paired with.
        attempt: The log to judge.

    Returns:
        The reason more work is needed, or `None` where this attempt is the answer.
    """
    return (
        _redirected(manifest, attempt)
        or _incomplete_reason(attempt, task.samples * task.epochs)
        or _reshaped(manifest, task, attempt)
    )


def answers_shape(manifest: Manifest, task: ManifestTask, attempt: LogAttempt) -> bool:
    """Whether an attempt was produced under the shape the run is now asking for.

    Both halves of shape: which samples were run (`_reshaped`) and what the run talked to (`_redirected`).

    Public because `launch` asks the same question of two directories a tend does not — of `logs-archive/`, before calling a restored log a reason the work does not run again, and of `logs/`, before counting a task among those a reshape costs. Asking it there rather than re-deriving it is what keeps the launch's preview and the tend's verdict the same answer.

    Args:
        manifest: Desired state, whose overrides and options say what shape is being asked for.
        task: The manifest row this attempt is paired with.
        attempt: The log to judge.

    Returns:
        Whether the attempt's samples are answers to the question now being asked.
    """
    return (
        _redirected(manifest, attempt) is None
        and _reshaped(manifest, task, attempt) is None
    )


def _reshaped(
    manifest: Manifest, task: ManifestTask, attempt: LogAttempt
) -> IncompleteReason | None:
    """Whether the log answers the question the run is now asking.

    Compares the run's *effective* selection — the override where there is one, the definition's own value otherwise — against what the log recorded running with. Both halves matter: an override is how a re-launch changes the slice, and `options` is how an edit to the definition does, and neither moves the identifier.

    **Silent where the manifest cannot say.** A field `options` does not carry is a field a manifest captured by an older inspect never recorded, and reading its absence as `None` would call a shuffled run stale on the day the reader is upgraded — permanently, since the next run records the same nothing. So the definition side is consulted only where the key is present; an override is always comparable, because Steward wrote it.
    """
    for name in SELECTION:
        override = getattr(manifest.overrides, name, None)
        if override is None and name not in manifest.options:
            continue
        wanted = override if override is not None else manifest.options[name]
        if name == "sample_id" and wanted is not None:
            # a `task:id` selector belongs to one task, and `eval_run` strips it
            # per task before the log records what ran -- so an unresolved list
            # compares against a resolved one and never matches
            wanted = resolve_task_sample_ids(task.name, wanted)
        if _selection(wanted) != _selection(attempt.selection.get(name)):
            return IncompleteReason.RESHAPED
    return None


def _redirected(manifest: Manifest, attempt: LogAttempt) -> IncompleteReason | None:
    """Whether an override has pointed the task at something else since the log was written.

    `sandbox` and `model_base_url` are identity-neutral and plainly affect results — a different image or a different gateway is a different answer to the same question. Upstream records that gap as inherited from task identity rather than created here; what Steward can do about it is refuse to call such a log settled, and refuse to resume it (`IncompleteReason.REDIRECTED`).

    **Only an override is compared, and only against what it actually said.** The definition's own values are not in `options`, so there is nothing to compare them with — an edit to either is invisible here, as it is to the identifier. And a log records the sandbox *resolved*: `--sandbox docker` becomes `docker:compose.yaml` where the task directory holds one, so comparing the config half of a type-only override would mark every such run stale forever. A type-only override is therefore compared on its type alone; one that names a config suppresses that resolution upstream and is compared whole.
    """
    overrides = manifest.overrides
    if overrides is None:
        return None

    if (
        overrides.model_base_url is not None
        and overrides.model_base_url != attempt.model_base_url
    ):
        return IncompleteReason.REDIRECTED

    # the same normalisation `resolve_task_sandbox` applies, so that the two
    # spellings of one sandbox -- `docker` and `SandboxEnvironmentSpec("docker")`
    # -- are the one value here that they are there
    sandbox = resolve_sandbox_environment(overrides.sandbox)
    if sandbox is not None:
        recorded = attempt.sandbox
        if sandbox.config is None:
            wanted = sandbox.type
            recorded = None if recorded is None else recorded.partition(":")[0]
        else:
            wanted = f"{sandbox.type}:{sandbox.config}"
        if wanted != recorded:
            return IncompleteReason.REDIRECTED

    return None


def _selection(value: Any) -> tuple[str, ...] | None:
    """One selection value, comparable across a round trip through JSON.

    A `(0, 5)` limit is a tuple in a manifest and a list in a log header, and a `sample_id` is a scalar or a list depending on how it was said. Neither difference is a change to what runs.
    """
    if isinstance(value, (list, tuple)):
        items: list[Any] = list(cast(list[Any], value))
        return tuple(str(item) for item in items)
    return None if value is None else (str(value),)


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


def read_attempt(location: str) -> LogAttempt:
    """Read one log's header as an attempt, for a log no observation covers.

    **The store is the caller, and it is the third directory to need this.** `observe_logs` answers for a *directory*, which is what `logs/` and `logs-archive/` are; a reuse store hands back individual logs from wherever it indexed them, and a launch has to ask `answers_shape` of one before copying it in. Reading a whole directory to judge one file would be the wrong shape and, for a table pointing at many prefixes, not even possible.

    Args:
        location: The log to read.

    Returns:
        The attempt, with no `mtime` — the field exists for the turn cache's staleness check, and nothing caches a log it is about to copy.

    Raises:
        OSError: The log could not be read.
        ValueError: It read and was not a log.
    """
    return _attempt(location, None, read_eval_log(location, header_only=True))


def _attempt(location: str, mtime: float | None, header: EvalLog) -> LogAttempt:
    results = header.results
    headline, headline_name = _headline(header)
    return LogAttempt(
        location=location,
        identifier=task_identifier(header, None),
        created=header.eval.created,
        status=header.status,
        invalidated=header.invalidated,
        error=header.error.message if header.error is not None else None,
        error_traceback=header.error.traceback if header.error is not None else None,
        total_samples=results.total_samples if results is not None else 0,
        completed_samples=results.completed_samples if results is not None else 0,
        epochs=header.eval.config.epochs,
        task=header.eval.task,
        task_id=header.eval.task_id,
        eval_id=header.eval.eval_id,
        mtime=mtime,
        headline=headline,
        headline_name=headline_name,
        selection={
            name: value
            for name, value in (
                ("limit", header.eval.config.limit),
                ("sample_id", header.eval.config.sample_id),
                ("sample_shuffle", header.eval.config.sample_shuffle),
            )
            if value is not None
        },
        sandbox=_sandbox(header),
        model_base_url=header.eval.model_base_url,
        limits=_limits(header),
    )


def _sandbox(header: EvalLog) -> str | None:
    """The log's sandbox in the `type:config` form the command line uses.

    Spelled out rather than `str(spec)`, which is pydantic's repr (`type='docker' config=None`) and would compare against nothing.
    """
    sandbox = header.eval.sandbox
    if sandbox is None:
        return None
    if sandbox.config is None:
        return sandbox.type
    return f"{sandbox.type}:{sandbox.config}"


def _headline(header: EvalLog) -> tuple[float | None, str | None]:
    """The metric the task declared, or the first metric of the first score.

    Both come from `headline_metric`, which reads the headline resolved at scoring time, falls back to the task's declaration for a log written before that existed, and falls back again to the convention when nothing was declared. Steward applies no rule of its own: two readers of one log agreeing is the whole point of the field, and a second opinion here would be the disagreement it was added to end.
    """
    resolved = headline_metric(header)
    if resolved is None:
        return None, None
    return float(resolved.metric.value), f"{resolved.score.name}/{resolved.name}"


LIMITS = {
    "turns": "turn_limit",
    "messages": "message_limit",
    "tokens": "token_limit",
    "time": "time_limit",
    "working": "working_limit",
    "cost": "cost_limit",
}
"""Per-sample budgets: the name a column is labelled with, and the `EvalConfig` field it comes from.

Spelled out rather than derived by appending `_limit`, because the two do not agree — a *turn* limit is `turn_limit` and its column is `turns` — and a name built by concatenation reads every one of these as absent without saying so.
"""


def _limits(header: EvalLog) -> dict[str, int]:
    """Whichever per-sample budgets the task declared."""
    config = header.eval.config
    declared = {name: getattr(config, field, None) for name, field in LIMITS.items()}
    return {
        name: int(value)
        for name, value in declared.items()
        if isinstance(value, float | int) and not isinstance(value, bool) and value > 0
    }
