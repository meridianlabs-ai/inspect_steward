"""What the fleet costs the machine, read from the process table.

Two claims, and both are about what happens when the input is not what a caller
naively assumes. A packed worker names one process from several rows, so the
same pid arrives repeatedly and must be counted once. And a process can exit
between being listed and being read, which this must survive rather than
report — a `status` that raises because a worker finished while it was looking
is worse than one that leaves a figure out.

This process is the subject, because a process that is definitely alive and
definitely has a resident set is the one thing a test can count on.
"""

import os

from inspect_steward._worker import process_usage


def test_a_pid_repeated_is_a_process_counted_once() -> None:
    # a packed worker reports a row per task and every one names the same
    # process, so a caller cannot be asked to deduplicate before it asks
    once = process_usage([os.getpid()])
    again = process_usage([os.getpid()] * 40)

    # the *count* is the claim, and comparing the two resident sets is not a
    # second way of making it: this process allocates between the two calls,
    # so an equality there fails on ordinary work rather than on a bug
    assert once.processes == again.processes == 1
    assert once.rss > 0 and again.rss > 0


def test_a_process_that_is_not_there_contributes_nothing() -> None:
    """And does not raise, which is the whole of this module's error handling.

    A pid that cannot be read is skipped rather than zeroed: contributing
    nothing to the total is exactly what a process that is no longer running
    contributes to the machine.
    """
    # 0 is not a process anybody can read; the live pid alongside it is what
    # distinguishes *skipped one* from *gave up on all of them*
    usage = process_usage([0, os.getpid()])

    assert usage.processes == 1
    assert usage.rss > 0
    assert process_usage([0]).processes == 0
    assert process_usage([]) == process_usage([0])
