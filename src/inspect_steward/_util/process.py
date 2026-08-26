"""Reading the process table, and the one thing that reliably goes wrong doing it.

`_worker.inflight` sweeps the process table for each worker's selection marker, which is how a worker that has not yet reached its eval is found at all. The sweep lives here rather than inline because the exception tuple below is subtle enough to be worth stating once, in a place a second caller will find it — a timer backend that scanned for its own marker was the second caller, and its removal does not make the guard less load-bearing for the first.
"""

from collections.abc import Iterator
from dataclasses import dataclass

import psutil

UNREADABLE = (psutil.Error, OSError, SystemError)
"""What reading another process can raise.

`SystemError` is the surprising member, and it is not theoretical. On macOS, `proc_environ` against a process that exits between the listing and the read returns with a Python exception already set, which psutil surfaces as a bare `SystemError` rather than as one of its own — so a sweep that catches only `psutil.Error` fails intermittently, and fails more often the busier the machine is. None of the three means anything except *this process cannot be read*, which for every caller here is the same as *this is not one of ours*.
"""


@dataclass(frozen=True)
class ProcessInfo:
    """One process, as a marker sweep needs it.

    A snapshot rather than a live `psutil.Process`, because the whole difficulty is that the process may be gone by the time anybody asks a second question of it. Everything that can raise is read once, under one guard.
    """

    pid: int
    ppid: int
    environ: dict[str, str]


def process_table() -> Iterator[ProcessInfo]:
    """Every process whose identity and environment could be read.

    Processes that could not be read are skipped rather than reported: they are gone, they are zombies, or they belong to another user, and none of the three is ever a process of Steward's.

    **The yield is deliberately outside the guard.** A generator suspended inside a `try` catches anything thrown in at the yield, which would silently swallow a consumer's own `psutil.Error` and turn a real failure into a skipped row.

    Returns:
        One entry per readable process, in no particular order.
    """
    for process in psutil.process_iter():
        try:
            info = ProcessInfo(
                pid=process.pid,
                ppid=process.ppid(),
                environ=process.environ(),
            )
        except UNREADABLE:
            continue
        yield info


__all__ = ["UNREADABLE", "ProcessInfo", "process_table"]
