"""Which of a store's answers a run may take: the read half of rung 2.

**One predicate, two callers.** A launch asks so it can copy the log in (`_launch.launch._reuse`); a rehearsal asks so it can leave the task out (`_smoke.run`). Both have to reach the same answer about the same store, or a smoke would skip a task the launch then runs — so the question is asked here and nowhere else, and the copy stays with the launch.

**A task identifier is not a promise about the results, and the store searches on nothing else.** `task_identifier` hashes the solver plan, generate config, model args, roles, version and execution limits — and pointedly *not* the sample count, the epochs or the selection, so that raising any of them leaves existing logs resumable rather than orphaning them. A store is therefore free to hand back a log for the same identifier that ran a different slice or fewer samples, and taking one would leave the task `INCOMPLETE`, the next tend queuing it, and the launch having already said the work does not run here.

**Every candidate, because the store's own ranking cannot see the question.** It orders by size and recency, which is all a manifest-blind index can do — so the log it puts first may be the one that answers a different slice while the one behind it matches exactly. Checking only the front of the list turned this filter into a veto: it rejected the best-ranked log and never found out the store had what it was asked for.

**The predicate is `observe`'s own**, not a second one assembled from the same ingredients. `incomplete_reason` is what `observe_tasks` classifies a task with, so a log this accepts is a log the next tend calls complete — which is the whole claim being made. A near copy of it drifted the way near copies do: it compared `completed_samples` where observation compares `total_samples`, so a signed log carrying samples an operator accepted as errored was publishable by one rule and unreusable by the other.

A log that fails is not refused so much as not *claimed*. It stays in the store, where a run asking a different question will match it.
"""

from collections.abc import Callable, Sequence, Set
from pathlib import Path

from .._evalset.manifest import Manifest, ManifestTask
from .._evalset.observe import incomplete_reason, read_attempt
from .store import open_store


def satisfied(
    manifest: Manifest,
    wanted: Set[str],
    location: str,
    *,
    root: Path,
    log: Callable[[str], None],
) -> dict[str, str]:
    """Which wanted tasks the store answers, and with what.

    Args:
        manifest: The run the question is about.
        wanted: Task identifiers to ask for. Every one of them is the manifest's.
        location: The store, as `store_location` resolved it.
        root: The workspace root a relative location is resolved against.
        log: Where a candidate that would not read, or did not answer, is noted. The operational log rather than the caller's failures: a store holding a near-miss is worth being able to find out about and is not something the operator did wrong.

    Returns:
        Task identifier to the source that answers it, for every wanted identifier the store could satisfy. The rest are absent rather than `None`.

    Raises:
        StoreError: The store could not be opened or searched. Left to the caller, because a launch and a rehearsal degrade in different words.
    """
    if not wanted:
        return {}
    found = open_store(location, root=root).search(wanted)
    rows = {task.identifier: task for task in manifest.tasks}
    chosen: dict[str, str] = {}
    for identifier, candidates in sorted(found.items()):
        source = _chosen(manifest, rows[identifier], candidates, log)
        if source is not None:
            chosen[identifier] = source
    return chosen


def _chosen(
    manifest: Manifest,
    task: ManifestTask,
    candidates: Sequence[str],
    log: Callable[[str], None],
) -> str | None:
    """The first of a store's candidates that answers what this run asks, or `None`."""
    for source in candidates:
        try:
            candidate = read_attempt(source)
        # the store's own rule, applied at the store's own boundary: this reads
        # a file the store named and the store may be a remote one, so the
        # failures available here are the backend's hierarchy rather than
        # Python's. A candidate that will not read is one candidate skipped
        except Exception as ex:
            log(f"{source} would not read from the store: {ex}")
            continue
        if (reason := incomplete_reason(manifest, task, candidate)) is None:
            return source
        # quietly, and to the log rather than to the launch: a store holding a
        # near-miss is worth being able to find out about and is not something
        # the operator did wrong
        log(
            f"{source} matches {task.key} by identifier and does not answer what "
            f"this run asks of it ({reason.value}) — not reused"
        )
    return None
