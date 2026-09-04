"""What the fleet is costing the machine, read from the process table.

Beside `live.py` rather than inside it, because the two answer different questions from different sources and fail independently. That module asks a worker's control socket *what is this eval doing*; this asks the kernel *what is this process costing*. A worker too busy to answer its socket still has a resident set, and a worker that has just exited has neither.

**Per process, never per task.** A packed worker runs several tasks in one process, so a figure summed per task multiplies one process's memory by the number of tasks it happens to hold — which at a batch size of five hundred is not a rounding error. Every pid is counted once, however many rows named it.

**CPU is an average since the process started, and every rendering of it says so.** `cpu_percent()` measures an *interval*, which means either blocking for one or keeping state between calls, and `status` can do neither: it is a read somebody types on a whim, holds nothing between invocations, and must not pause to watch a process. Cumulative CPU time over wall clock since `create_time()` is one stateless read, and it answers what the figure is for — whether the fleet is working or waiting on the network. It is slow to react to a change, which is the accepted cost.

Nothing here raises. A process that exited between being listed and being read contributes nothing, exactly as it contributes nothing to the machine.
"""

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import psutil


@dataclass(frozen=True)
class ProcessUsage:
    """What a set of processes is holding, right now."""

    processes: int = 0
    """How many were actually read. Fewer than were asked for where one exited mid-read — and the count is reported rather than the count requested, since the memory below is the memory of *these*."""

    rss: int = 0
    """Resident memory, in bytes, summed over the processes read."""

    cores: float = 0.0
    """CPU cores' worth of work, summed over the processes read.

    An average over each process's whole life rather than an instantaneous rate — see the module docstring. `1.0` is one core saturated since startup; a fleet of eight workers waiting on a model averages well under one between them.
    """

    seconds: dict[int, float] = field(default_factory=dict[int, float])
    """Cumulative CPU seconds per pid, over each process's whole life.

    The raw material for a *windowed* measure, which this module cannot compute and the tend loop can: a turn records these beside its observation, and the next turn's delta over the inter-tend interval is a real utilization figure — reactive where `cores` is a lifetime average, and still stateless here (workflow.md, the tuning loop's CPU gate).
    """

    rss_by_pid: dict[int, int] = field(default_factory=dict[int, int])
    """Resident memory per pid, for a caller sharing a process's figure out among the tasks it holds."""

    cores_by_pid: dict[int, float] = field(default_factory=dict[int, float])
    """Cores per pid, the same way."""


def process_usage(pids: Iterable[int]) -> ProcessUsage:
    """Read memory and CPU for a set of processes.

    Args:
        pids: Process ids, deduplicated here — a caller holding one row per task must not have to remember that several of them share a process.

    Returns:
        The totals over every pid that could be read. All zeros where none could, which a caller renders as *no block* rather than as *no usage*.
    """
    processes = 0
    rss = 0
    cores = 0.0
    seconds: dict[int, float] = {}
    rss_by_pid: dict[int, int] = {}
    cores_by_pid: dict[int, float] = {}
    now = time.time()

    for pid in sorted(set(pids)):
        try:
            process = psutil.Process(pid)
            # one syscall's worth of caching across the three reads below, which
            # matters at a fleet of forty and costs nothing at a fleet of one
            with process.oneshot():
                resident = process.memory_info().rss
                times = process.cpu_times()
                started = process.create_time()
        except (psutil.Error, OSError):
            # exited, or a platform that will not answer. Skipped rather than
            # zeroed: a process contributing nothing to the total is the same
            # thing as a process that is no longer there
            continue

        processes += 1
        rss += resident
        rss_by_pid[pid] = resident
        seconds[pid] = times.user + times.system
        if (elapsed := now - started) > 0:
            cores_by_pid[pid] = seconds[pid] / elapsed
            cores += cores_by_pid[pid]

    return ProcessUsage(
        processes=processes,
        rss=rss,
        cores=cores,
        seconds=seconds,
        rss_by_pid=rss_by_pid,
        cores_by_pid=cores_by_pid,
    )
