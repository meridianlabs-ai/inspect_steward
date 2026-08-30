"""What launching would change, and which half of it needs a person.

A launch captures a fresh manifest and commits it as desired state. The delta is what stands between those two acts, and it exists because of an asymmetry that is sharper than it first looks: **adding work is what the human just asked for, and removing work from `logs/` could equally be a typo.** A one-character change to a task arg produces a new identifier and reads identically to a deliberate removal, except that one of the two quietly buys a re-run of everything (workflow.md §2.3).

**The gate is a refusal to commit, and it can only be that.** `reconcile` archives orphans with no acceptance parameter and should never grow one — once desired state says a task is not in the eval set, converging toward that is bookkeeping rather than a decision. So the moment `write_manifest` lands a manifest that orphans tasks, the 02:00 tend archives them with nobody present. The consent has to be taken *before* the commit or it cannot be taken at all.

**Pure, and that is what makes it worth having.** No filesystem, no clock, no processes: the caller reads the two manifests, the log directory, the archive, and the process table, and this decides. So every row can be exercised against synthesized state, which matters for the one row nobody wants to discover is wrong in production.

**A commit can cost results without archiving anything, which is why `additive` is a property of the whole delta rather than a filter over its rows.** Moving the definition's `log_dir` leaves every identifier untouched — so every row above is silent — while putting the run's results in a directory the next tend no longer reads. Nothing is archived, nothing is deleted, and the whole sweep runs again. It is the archiving case in a different costume, and it is gated by the same predicate for that reason.

**A relocation also costs the fleet.** A worker's destination is written into its selection document when it spawns and cannot be changed afterwards, so every worker still running is producing a log for a directory the run has just stopped reading. Its task is not respawned while it lives — the loop can see it running — and once it exits its work is nowhere the loop looks, so the task starts again from nothing. Stopping them is therefore part of what a relocation *is*, not tidying afterwards.

**Five rows, and the fifth is why the archive is a cache.** Edit a task's args, launch, decide the edit was wrong, revert, launch again: the original identifier is back and its log is sitting in `logs-archive/`, so satisfying it is a move rather than a re-run (workflow.md §2.2). Without the restore row that revert costs the same as the mistake did.

**Identifiers are compared, never parsed.** Supersession asks *did this task survive under a different configuration*, and the manifest answers with fields — `file`, `name`, `model` — rather than with the shape of the identifier string. The identifier's format is upstream's to change; these three are a contract.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .._evalset.manifest import (
    REDIRECTION,
    SELECTION,
    Manifest,
    ManifestTask,
)
from .._evalset.observe import ObservedLogs
from .._schedule import RunningWorker


class Change(StrEnum):
    """What launching would do to one task."""

    ADD = "add"
    """Not in the committed manifest. New work, and the only kind the agent may commit unasked."""

    EXTEND = "extend"
    """Same identifier, more work than before — raised epochs, or a grown dataset. Additive, because nothing already in `logs/` stops counting: `epochs` is absent from `task_identifier`'s hash, so raising it keeps the identity and lifts the bar the existing log is measured against, and the next tend resumes rather than restarts."""

    REMOVED = "removed"
    """Gone from the definition, with nothing of its name and model left. Archives."""

    SUPERSEDED = "superseded"
    """Gone from the definition, but a task of the same name and model is still there under a different configuration — so this is what an edit displaced rather than what somebody deleted. Archives, and the work comes back as an `ADD`."""

    RESTORE = "restore"
    """Asked for, and a log for it is sitting in the archive. A move rather than a re-run."""


ARCHIVING = frozenset({Change.REMOVED, Change.SUPERSEDED})
"""The rows that move something out of `logs/`, and therefore the ones the gate is about.

Both are orphans, which is what dissolves an apparent contradiction between workflow.md §2.3 and plan.md step 13: an args edit produces a *new* identifier, so the old one is not a live task with a superseded attempt — it is an orphan, and `reconcile._archiving` already treats it as one. What waits for signoff instead is multiple attempts of a still-live identifier, which is a different thing entirely and is not in this vocabulary.
"""


@dataclass(frozen=True)
class Relocation:
    """The run's results are in one directory and its next tend would read another.

    Not a row, because it is not about a task: every task in the manifest can be identical and unaffected, and the loss is that the *directory* moved out from under them. Reported as one fact with a count attached.
    """

    old: str
    """Where the committed manifest's results are."""

    new: str
    """Where the captured manifest would put them."""

    stranded: int
    """Tasks the new manifest wants that have results in `old` and none in `new` — exactly the set that would run a second time, and exactly the set whose first results would be left behind."""

    workers: tuple[str, ...] = ()
    """Every worker alive when this was computed, by stem.

    **All of them, whatever their task's row says.** A worker's log directory is fixed in its selection document when it spawns and it cannot be told otherwise, so any worker running when the manifest is committed is writing into the directory being left behind — including one running a task both manifests name, and one running a task neither does. Left alone it spends hours producing a log nothing will read, and its task runs again from nothing once it exits."""


@dataclass(frozen=True)
class Reshaped:
    """The run would ask for different samples than its results were produced from.

    Not a row, for the same reason a relocation is not: every task can be identical and unaffected, and what changed is the *slice* underneath all of them. `limit`, `sample_id` and `sample_shuffle` are all identity-neutral, so nothing above notices — a `(0, 5)` limit changed to `(5, 10)` keeps every identifier and every sample count, and reports as *nothing to change* while quietly signing the previous subset off as the answer.

    **Reported rather than gated.** A relocation is usually an accident of an edited `log_dir`; this is somebody typing `--limit` and meaning it. Nothing leaves `logs/` either — the superseded attempts stay where they are, as they do for any re-run — so `--accept-archive` would be both unnecessary and the wrong word. What was missing was the sentence, not the consent.
    """

    fields: tuple[str, ...]
    """Which shaping fields differ — the slice first, then what the run talks to."""

    affected: int
    """Tasks holding results that would be run again."""

    workers: tuple[str, ...] = ()
    """Every worker alive when this was computed, by stem.

    **All of them, for the reason a relocation stops all of them.** A worker's selection is fixed in the document it spawned with; it is running the old slice and cannot be told otherwise. Left alone it finishes, writes a log the next tend reads as `reshaped`, and its task runs again from nothing — so the only thing its remaining hours buy is a superseded attempt.

    Not conditioned on which tasks it holds, because a slice is eval-set level: every worker in the fleet is running the old one."""


@dataclass(frozen=True)
class TaskChange:
    """One task, and what launching would do to it."""

    change: Change
    identifier: str
    key: str
    """Display key, from whichever manifest this task came out of."""

    samples: int
    """Samples the row is about: what the new manifest asks for, or for an archiving row what the old one asked for — the work being set aside, which is also the work an `ADD` beside it will redo."""

    logs: tuple[str, ...] = ()
    """Logs this row would move. In `logs/` for the archiving rows, in `logs-archive/` for a restore. Empty where a task has no results yet, which is the common case for `ADD`."""

    worker: str | None = None
    """A worker still running this task, which archiving it would have to stop first. `None` when nothing is running it."""

    epochs: tuple[int, int] | None = None
    """`(before, after)` for an `EXTEND`, and `None` otherwise. Carried because it is the change a reader can act on — *epochs 1 → 3* says what happened where a sample count only says it got bigger."""


@dataclass(frozen=True)
class Delta:
    """Everything launching would change."""

    changes: list[TaskChange] = field(default_factory=list[TaskChange])
    """Every row, ordered by `Change` and then by display key — additive rows first, so the reader meets what they asked for before what it costs."""

    first: bool = False
    """Whether this is the first launch here, so there was no committed manifest to compare against. Every task is then an `ADD` and the delta needs no acceptance, which is right but is worth being able to say out loud rather than inferring from *nothing is being archived*."""

    relocated: Relocation | None = None
    """The log directory moved, or `None` where it did not. Not additive — see the module docstring."""

    reshaped: Reshaped | None = None
    """The dataset slice changed under identical identifiers, or `None` where it did not."""

    def of(self, change: Change) -> list[TaskChange]:
        """The rows of one kind, in order.

        Args:
            change: The kind to select.

        Returns:
            Those rows.
        """
        return [row for row in self.changes if row.change == change]

    @property
    def archiving(self) -> list[TaskChange]:
        """Every row that would move a task's logs out of `logs/`."""
        return [row for row in self.changes if row.change in ARCHIVING]

    @property
    def additive(self) -> bool:
        """Whether committing this would cost nothing that already exists.

        The predicate behind both surfaces — the CLI's gate and the bound on the agent's autonomy — so that the answer cannot differ between them (workflow.md §2.3, *One predicate, two surfaces*).

        **Two ways to fail it, not one.** Archiving moves results out of `logs/`; relocating changes which directory `logs/` *means*, stranding them where they are and re-running the work. The second was missed at first because it produces no rows at all — every identifier survives a `log_dir` edit — which is precisely why the predicate is asked of the delta rather than counted off its table.
        """
        return not self.archiving and self.relocated is None

    @property
    def stopping(self) -> list[str]:
        """Workers that would have to be stopped, by stem.

        **Three reasons, deduplicated.** A worker is stopped because a task of its is leaving the manifest, because the directory it is writing to is, or because the slice it is running was replaced — and one worker can be all three. Signalling the same pid twice would be at best wasted and at worst aimed at whatever inherited the number, so the union is taken here rather than left to the caller.

        Archiving rows first, in row order, then the rest of the fleet in the order the scan reported it.

        A stem rather than a stem and a task list: which of a packed worker's tasks are actually going is `wholesale` and `leaving` together, and resolving that needs the worker's own identifiers, which live in the in-flight record rather than in a delta.
        """
        stems = [row.worker for row in self.archiving if row.worker is not None]
        seen = set(stems)
        stems.extend(
            worker
            for worker in self.wholesale_order
            if worker not in seen and not seen.add(worker)
        )
        return stems

    @property
    def leaving(self) -> set[str]:
        """Identifiers whose logs would be archived, and so whose workers have nothing left to do.

        What a *partial* stop is computed from: intersected with what a worker is actually running, it says which of its tasks to cancel and which to leave alone.
        """
        return {row.identifier for row in self.archiving}

    @property
    def wholesale(self) -> set[str]:
        """Workers to stop outright whatever they are running, by stem.

        Relocation and reshaping. Neither is about any task — the directory moved out from under all of them, or the slice did — so there is no subset to compute and every task the worker holds is going.
        """
        return set(self.wholesale_order)

    @property
    def wholesale_order(self) -> tuple[str, ...]:
        """`wholesale`, in the order the scan reported the fleet, for `stopping` to append."""
        return (self.relocated.workers if self.relocated else ()) + (
            self.reshaped.workers if self.reshaped else ()
        )

    @property
    def empty(self) -> bool:
        """Whether launching would change nothing at all — a re-launch of an unedited definition."""
        return not self.changes and self.relocated is None and self.reshaped is None


_ORDER = {
    Change.ADD: 0,
    Change.EXTEND: 1,
    Change.RESTORE: 2,
    Change.REMOVED: 3,
    Change.SUPERSEDED: 4,
}


def compute_delta(
    new: Manifest,
    old: Manifest | None,
    *,
    logs: ObservedLogs,
    archived: ObservedLogs,
    running: Sequence[RunningWorker],
    stranded: ObservedLogs | None = None,
) -> Delta:
    """What launching this manifest over that one would do.

    Args:
        new: The manifest just captured.
        old: The committed manifest, or `None` on a first launch.
        logs: The captured manifest's log directory, as `observe_logs` read it. Supplies the logs an archiving row would move.
        archived: Its archive, read the same way. Supplies the restore rows.
        running: The workers in flight, as `resolve_inflight` reported them. The fleet rather than an identifier-to-stem index, because a relocation stops *every* worker and two workers on one identifier — which a deleted `.steward/` mid-run can produce — would collapse to one in a mapping, leaving the other writing into the abandoned directory.
        stranded: The **committed** manifest's log directory, when it is not the same directory as `logs`. `None` when the two agree, which is the ordinary case. Each `ObservedLogs` carries its own `log_dir`, so the pair is enough to describe the move as well as to count what it would cost.

    Returns:
        Every row, whether this was a first launch, and the relocation if there is one.
    """
    wanted = {task.identifier: task for task in new.tasks}
    committed = {task.identifier: task for task in (old.tasks if old else [])}
    # a row names *a* worker running its task, which is all a row can say; the
    # fleet-wide question is answered from the sequence itself. One worker can
    # appear against several rows once a run is packed, which is the case
    # `Delta.stopping` deduplicates
    by_task = {
        identifier: worker.worker
        for worker in running
        for identifier in worker.identifiers
    }

    changes = [
        *_added(wanted, committed, by_task),
        *_extended(wanted, committed),
        *_restorable(wanted, logs, archived),
        *_archived(wanted, committed, logs, by_task),
    ]
    changes.sort(key=lambda row: (_ORDER[row.change], row.key))
    return Delta(
        changes=changes,
        first=old is None,
        relocated=_relocation(wanted, logs, stranded, running),
        reshaped=_reshaped(new, old, wanted, logs, running),
    )


def _reshaped(
    new: Manifest,
    old: Manifest | None,
    wanted: Mapping[str, ManifestTask],
    logs: ObservedLogs,
    running: Sequence[RunningWorker],
) -> Reshaped | None:
    """Whether the run's shape moved out from under its results.

    Compares the two manifests' *effective* values — the override where there is one, the definition's own otherwise — because either can move them and neither moves an identifier. `observe` reaches the same conclusion per task, from the logs themselves, and is what actually re-runs them; this exists so that `launch` says so at the moment somebody types it rather than leaving them to notice a full re-run at the next tend.

    **Both kinds, because both re-run.** The dataset slice (`limit`, `sample_id`, `sample_shuffle`) is what makes a log answer a different question; `sandbox` and `model_base_url` leave the question alone and make every answer stale, which `observe` treats more harshly still — a redirected task is spawned without its prior log rather than resumed. A launch that reported only the first would have promised *nothing to change* for a changed gateway and stopped no workers, which is the same silence the slice comparison was added to end.

    Args:
        new: The manifest just captured.
        old: The committed manifest, or `None` on a first launch.
        wanted: The captured manifest's tasks by identifier.
        logs: The run's log directory.
        running: The fleet as the scan found it, every member of which is running the old slice.

    Returns:
        What changed and what it costs, or `None` where nothing did.
    """
    if old is None:
        return None
    changed = tuple(
        name
        for name in (*SELECTION, *REDIRECTION)
        if _selected(new, name) != _selected(old, name)
    )
    if not changed:
        return None
    return Reshaped(
        fields=changed,
        affected=sum(1 for identifier in wanted if logs.attempts.get(identifier)),
        workers=tuple(worker.worker for worker in running),
    )


def _selected(manifest: Manifest, name: str) -> Any:
    """One shaping field's effective value, comparable between two manifests.

    A field `options` does not record can still be compared here, unlike in `observe`: both sides come from a manifest rather than from a log, so an absent key is absent on both and the comparison is silent by construction. `sandbox` and `model_base_url` are exactly that case — the definition's own values are recorded nowhere, so only an override moves them.
    """
    override = getattr(manifest.overrides, name, None)
    return override if override is not None else manifest.options.get(name)


def _relocation(
    wanted: Mapping[str, ManifestTask],
    logs: ObservedLogs,
    stranded: ObservedLogs | None,
    running: Sequence[RunningWorker],
) -> Relocation | None:
    """The move, and how much of the run it would cost.

    **Counted as *wanted, there, and not here*.** A task whose results are already in the new directory — someone copied them across before launching, which is the sensible thing to do — is not stranded and would not re-run, so counting the old directory's contents wholesale would refuse a launch that costs nothing. And a task the new manifest does not want is leaving anyway, by the archiving rows.
    """
    if stranded is None:
        return None
    return Relocation(
        old=stranded.log_dir,
        new=logs.log_dir,
        stranded=sum(
            1
            for identifier in wanted
            if stranded.attempts.get(identifier) and not logs.attempts.get(identifier)
        ),
        workers=tuple(worker.worker for worker in running),
    )


def _added(
    wanted: Mapping[str, ManifestTask],
    committed: Mapping[str, ManifestTask],
    running: Mapping[str, str],
) -> list[TaskChange]:
    """Tasks the new manifest names that the old one did not."""
    return [
        TaskChange(
            change=Change.ADD,
            identifier=task.identifier,
            key=task.key,
            samples=task.samples * task.epochs,
            # a task nothing committed that something is nonetheless running:
            # a worker spawned from a manifest that has since been replaced,
            # which is exactly what a `.steward/` deletion mid-run produces
            worker=running.get(task.identifier),
        )
        for identifier, task in wanted.items()
        if identifier not in committed
    ]


def _extended(
    wanted: Mapping[str, ManifestTask], committed: Mapping[str, ManifestTask]
) -> list[TaskChange]:
    """Tasks in both manifests that now ask for more work than they did.

    Strictly *more*: a task whose epochs were lowered keeps every log it has and is already satisfied, so there is nothing for a launch to report and nothing for a tend to do. Reporting a reduction as a change would invite the reader to look for work that is not going to happen.
    """
    rows: list[TaskChange] = []
    for identifier, task in wanted.items():
        before = committed.get(identifier)
        if before is None:
            continue
        required = task.samples * task.epochs
        if required <= before.samples * before.epochs:
            continue
        rows.append(
            TaskChange(
                change=Change.EXTEND,
                identifier=identifier,
                key=task.key,
                samples=required,
                epochs=(
                    (before.epochs, task.epochs)
                    if before.epochs != task.epochs
                    else None
                ),
            )
        )
    return rows


def _restorable(
    wanted: Mapping[str, ManifestTask],
    logs: ObservedLogs,
    archived: ObservedLogs,
) -> list[TaskChange]:
    """Tasks the new manifest asks for whose results are sitting in the archive.

    **The question is about the new manifest and the two directories, and not at all about the old manifest.** A task committed before can still have its logs in the archive — a launch archived them and no tend has re-run it yet, or somebody reverted twice — and that is exactly the case where a restore saves the most. So membership in the old manifest is not consulted.

    **What is consulted is `logs/`.** A task already holding an attempt in the run's own directory is satisfied, and pulling a second copy back from the archive would add an attempt nobody asked for — which `observe_logs` would then have to arbitrate between, for no gain. The archive is a fallback for results the directory does not have.
    """
    return [
        TaskChange(
            change=Change.RESTORE,
            identifier=identifier,
            key=task.key,
            samples=task.samples * task.epochs,
            logs=tuple(attempt.location for attempt in archived.attempts[identifier]),
        )
        for identifier, task in wanted.items()
        if archived.attempts.get(identifier) and not logs.attempts.get(identifier)
    ]


def _archived(
    wanted: Mapping[str, ManifestTask],
    committed: Mapping[str, ManifestTask],
    logs: ObservedLogs,
    running: Mapping[str, str],
) -> list[TaskChange]:
    """Tasks the old manifest named that the new one does not.

    Split on whether the definition still has a task of that name and model. Both archive; the distinction is what a reader needs to tell *I deleted this* from *I edited this*, and only the second one implies the work is about to be done again.
    """
    surviving = {(task.file, task.name, task.model) for task in wanted.values()}
    return [
        TaskChange(
            change=(
                Change.SUPERSEDED
                if (task.file, task.name, task.model) in surviving
                else Change.REMOVED
            ),
            identifier=identifier,
            key=task.key,
            samples=task.samples * task.epochs,
            logs=tuple(
                attempt.location for attempt in logs.attempts.get(identifier, [])
            ),
            worker=running.get(identifier),
        )
        for identifier, task in committed.items()
        if identifier not in wanted
    ]


__all__ = [
    "ARCHIVING",
    "Change",
    "Delta",
    "Relocation",
    "TaskChange",
    "compute_delta",
]
