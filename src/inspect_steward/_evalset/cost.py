"""What reading a definition costs, measured while it is being read.

**The measurement is free because the work is already being done.** Capture executes the definition and constructs every task in the eval set, datasets included, so that it can compute identifiers and sample counts. That is the most startup work anything does — so capture's peak memory bounds what a worker's startup costs, measured rather than guessed at ([scheduling.md](../../../design/scheduling.md), *Memory is assumed adequate*).

**A ceiling, and it is reported as one.** Early pruning means a worker constructs only the tasks it was selected to run ([configuration.md](../../../design/configuration.md) §6.2), so on a large set a worker's real startup is a fraction of this. The gap is the point rather than an inaccuracy: what capture measures is exactly what a worker pays when pruning does *not* fire — a definition building tasks without `@task`, or the recovery path after a bad match — and a bound that holds in the bad case is what an operator deciding how wide to run actually needs. Calling it a projection would invite the opposite reading, so the line says *at most*.

The figure is worth having because nothing else bounds it. `max_workers` unset means a process per task, so a five-hundred-task sweep asks for five hundred copies of whatever this measures. The operator chose unbounded and keeps that choice; what they get is the number, at the moment they are deciding.

Startup only. A worker also holds samples in flight, transcripts, and whatever a sandbox costs, none of which capture pays and none of which this describes.

**Sampled rather than taken from `getrusage`.** `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` is the obvious shortcut and is wrong here: it is a running maximum over *every* child this process has reaped, including their descendants. A Hawk definition shells out to `uv pip install` before it reaches `eval_set()`, so the shortcut would report a number about the installer and call it the eval set's. Polling the process tree attributes the peak to the tree that is being measured.

A sampler can miss a spike between polls, and that is accepted: this is a projection an operator reads, not a gate anything passes. Under-reporting is the safe direction for a figure whose only use is *this looks large*.
"""

import threading
from dataclasses import dataclass

import psutil

SAMPLE_INTERVAL = 0.05
"""Seconds between polls. Fine enough to catch a dataset load, coarse enough that a two-second capture pays about forty reads of a procfs entry."""


@dataclass(frozen=True)
class CaptureCost:
    """What executing a definition cost."""

    peak_rss: int | None
    """Peak resident memory of the capture process tree, in bytes, or `None` where it could not be measured (a process that finished before the first poll, or a platform that would not report it)."""


class _Sampler(threading.Thread):
    """Polls a process tree's resident memory until told to stop."""

    def __init__(self, pid: int) -> None:
        super().__init__(daemon=True)
        self._pid = pid
        self._done = threading.Event()
        self.peak = 0

    def run(self) -> None:
        try:
            process = psutil.Process(self._pid)
        except psutil.Error:
            return
        while True:
            self.peak = max(self.peak, _tree_rss(process))
            # wait rather than sleep, so the join below returns as soon as the
            # child is gone rather than after one more full interval
            if self._done.wait(SAMPLE_INTERVAL):
                # one last read: a definition that finished between polls would
                # otherwise be reported at whatever it had reached earlier, and
                # a fast capture is exactly when that matters
                self.peak = max(self.peak, _tree_rss(process))
                return

    def stop(self) -> int | None:
        self._done.set()
        self.join(timeout=SAMPLE_INTERVAL * 20)
        return self.peak or None


def _tree_rss(process: psutil.Process) -> int:
    """Resident memory of a process and its descendants, in bytes.

    Descendants included because a frontend is entitled to use them — Hawk runs its install as a child — and because the question being answered is what the *worker* will cost, which is a tree too.

    Zero for a process that has exited or that will not answer, which the caller reads as "nothing more to add" rather than as a measurement.
    """
    total = 0
    try:
        members = [process, *process.children(recursive=True)]
    except psutil.Error:
        return 0
    for member in members:
        try:
            total += member.memory_info().rss
        except psutil.Error:
            # gone between the listing and the read, which is ordinary for a
            # short-lived child and costs this sample only
            continue
    return total


def measure(pid: int) -> "_Sampler":
    """Start sampling a process tree. Call `stop()` for the peak."""
    sampler = _Sampler(pid)
    sampler.start()
    return sampler


def fleet_width(tasks: int, *, max_workers: int | None, max_tasks: int | None) -> int:
    """How many worker processes this run will have alive at its widest.

    Not what the next turn spawns, which is a fact about right now — the projection is about the shape the run converges to, since that is what an operator is deciding when they read it.

    Args:
        tasks: Tasks in the manifest.
        max_workers: Processes allowed, or `None` for a process per task.
        max_tasks: Tasks in flight allowed, or `None` for all of them.

    Returns:
        Peak concurrent worker processes.
    """
    if max_workers is not None:
        return min(max_workers, tasks)
    # unbounded workers means a process per task, so the task ceiling is what
    # bounds the process count too
    return min(max_tasks, tasks) if max_tasks is not None else tasks


def projection(peak_rss: int | None, width: int) -> str | None:
    """What a run of this width can cost to start, as a line for a person.

    `None` when nothing was measured, which is the state every manifest written before this existed is in — and the state a reader should see nothing about rather than a zero.

    **Phrased as a bound, because it is one.** See the module docstring: capture builds every task and a worker builds its own, so this is what the fleet costs when pruning does not fire rather than what it usually costs.

    **Compared against the machine's total memory, with one caveat left in a comment rather than fixed.** `psutil` reports the host's memory, so inside a container this over-states what is available. Reading the cgroup limit instead would reintroduce the cgroup-detection code deleted at step 17, for a line that is advisory — so the number is named as the machine's and a reader in a container knows to discount it.
    """
    if peak_rss is None:
        return None
    total = psutil.virtual_memory().total
    return (
        f"startup memory: at most {_gib(peak_rss)} per worker, "
        f"{_gib(peak_rss * width)} across {width} "
        f"worker{'' if width == 1 else 's'}, of {_gib(total)} on this machine"
    )


def _gib(value: int) -> str:
    gib = value / (1024**3)
    return f"{gib:.1f} GiB" if gib >= 0.1 else f"{value / (1024**2):.0f} MiB"
