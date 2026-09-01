"""How much of what landed was actually scanned.

**The number a scan finding cannot supply.** A census of what the scanners flagged says nothing about what they *reached*, and the two failures it cannot tell apart are the ones that matter: a run where every scanner answered and found nothing, and a run where the scanners never ran. Both produce an empty findings list, and a signature taken over the second says *nothing was flagged* about transcripts nothing ever looked at.

**Recorded rows against landed samples**, in the design's own words (scheduling.md §4.2) — and a row counts as recorded once *every* scanner has answered for its transcript, which is upstream's own resume predicate rather than a rule invented here: a sample two of three scanners reached is a sample the third will be sent back for. An **errored** row still counts. A scanner that threw is its own anomaly class (`scanerror:`), and counting it here as well would report one failure twice in two places that disagree about what to do with it.

**The numerator is free and the denominator is where the resume hazard lives.** Every scan row already carries the log it names and the transcript it is about, so `_scan.findings` accumulates the numerator inside the pass it was already making. The denominator is the header's `total_samples` — exact, and exactly wrong for one shape of task. After a retry the new log carries the samples that already succeeded, keeping their uuids, while their scan rows still name the *superseded* file: counting rows per-log undercounts by the whole reused population, and summing across logs overcounts by the attempts that were dropped. The only stable key is the sample uuid, which no header carries — so a resumed task pays one summaries read (`_evalset.instances.sample_uuids`) and its numerator is intersected against what its current log actually holds.
"""

from collections.abc import Collection, Mapping, Set
from dataclasses import dataclass, field

from .._evalset.observe import ObservedTasks


@dataclass(frozen=True)
class TaskCoverage:
    """One task's transcripts scanned against its samples landed."""

    scanned: int
    landed: int

    known: bool = True
    """Whether the numerator could be established at all.

    False for a resumed task whose current log would not read: the uuid set that would have made the count honest is unavailable, and every other number in reach is one about samples that may no longer be in the results. `scanned` is then `0` — the only figure that claims nothing — and the surfaces render it as unknown rather than as zero, because *nothing is known to be scanned* and *nothing was scanned* would send a reader looking for two different problems.
    """

    @property
    def complete(self) -> bool:
        """Whether every landed sample is accounted for. Never true where nothing could be verified."""
        return self.known and self.scanned >= self.landed


@dataclass(frozen=True)
class Coverage:
    """What the scanners reached, per task and run-wide.

    Empty for a run that scans nothing at all, which is what takes the column off the table rather than filling it with zeroes about a question nobody asked.
    """

    by_task: dict[str, TaskCoverage] = field(default_factory=dict[str, TaskCoverage])
    """Task identifier to its own pair. A task with no current log is absent — there is nothing landed to be over."""

    scanned: int = 0
    landed: int = 0
    """The run's totals, **over the tasks whose coverage could be verified**. A task whose numerator is unknown contributes to neither, because adding its landed samples to a denominator whose numerator is a guess would report a gap nobody measured."""

    @property
    def gap(self) -> int:
        """Landed samples no scanner has answered for, among the tasks that could be counted."""
        return max(0, self.landed - self.scanned)

    @property
    def unverified(self) -> tuple[str, ...]:
        """Tasks whose coverage could not be established at all, in table order."""
        return tuple(
            identifier for identifier, entry in self.by_task.items() if not entry.known
        )


def coverage(
    observed: ObservedTasks,
    recorded: Mapping[str, Set[str]],
    *,
    reused: Mapping[str, frozenset[str]],
    unverified: Collection[str] = (),
    scanning: bool,
) -> Coverage:
    """Fold the recorded transcripts against the landed samples.

    Args:
        observed: The log directory read against the manifest.
        recorded: Per task, the transcripts every scanner has answered for (`_scan.findings.ScanFindings.recorded`).
        reused: Per **resumed** task, the sample uuids its current log actually holds. A task absent from this mapping and from `unverified` is one whose rows and whose log agree exactly, which is most of them.
        unverified: Resumed tasks whose current log would not read, so the intersection could not be taken. **Not the same as absent**, and the difference is the whole of this argument's existence — see below.
        scanning: Whether this run scans at all. False produces an empty coverage, so a run with no scan material grows no column.

    Returns:
        The per-task pairs and the run's totals.

    **Three states, because two of them would silently agree on the wrong answer.** A task that was never resumed needs no intersection: its rows and its log name the same file, so `len(answered)` is exact. A task that was resumed and whose log read gets the intersection. A task that was resumed and whose log **would not read** gets neither — and falling back to `len(answered)` there is the specific failure this argument exists to prevent, because for a resumed task that number is the union across attempts. Four samples scanned under the old log, two of them replaced by re-runs nothing has scanned, reads as `4` against a denominator of `4`: a run reported as fully scanned on the strength of rows about samples that are no longer in it.
    """
    if not scanning:
        return Coverage()
    unknown = set(unverified)
    by_task: dict[str, TaskCoverage] = {}
    for task in observed.tasks:
        if task.current is None or task.current.total_samples <= 0:
            continue
        landed = task.current.total_samples
        answered = recorded.get(task.identifier, frozenset())
        held = reused.get(task.identifier)
        if held is not None:
            scanned, known = len(answered & held), True
        elif task.identifier in unknown:
            # nothing here can be verified, and the honest report of that is a
            # cell that says so rather than a number that happens to be
            # reassuring
            scanned, known = 0, False
        else:
            scanned, known = len(answered), True
        # **clamped, and the reason is a race rather than a fudge.** The
        # denominator comes from a header read taken at the top of the turn and
        # the numerator from a parquet folded partway down it, so a task still
        # running can legitimately have scanned a sample the header had not yet
        # counted. `51 of 50` is a true statement about two reads and a useless
        # one about a run
        by_task[task.identifier] = TaskCoverage(
            scanned=min(scanned, landed), landed=landed, known=known
        )
    counted = [entry for entry in by_task.values() if entry.known]
    return Coverage(
        by_task=by_task,
        scanned=sum(entry.scanned for entry in counted),
        landed=sum(entry.landed for entry in counted),
    )


__all__ = [
    "Coverage",
    "TaskCoverage",
    "coverage",
]
