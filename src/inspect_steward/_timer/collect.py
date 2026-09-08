"""Run a scheduled agent collect under a lock, so two never overlap.

The scheduler fires `<agent> exec` on an interval (`_cli.schedule`), and a scheduler is not a supervisor: cron starts a fresh process every interval whether or not the last one finished, and even the schedulers that singleton by job (launchd, systemd) only do so as a side effect this module should not depend on. A collect that outruns its interval must not have a second agent started on top of it — two agents reasoning about the same run and issuing `steward` verbs at once is a race nobody asked for.

So what the scheduler actually runs is not the agent directly but a thin Steward process (`steward schedule run`) that takes a non-blocking lock and only then execs the agent. If the lock is held, this tick is skipped rather than queued: a collect is a stateless read of *current* state, so the next fire sees everything a skipped one would have, and queuing would only pile agents up behind a slow one.

**An `flock` rather than a pidfile**, because the kernel releases an advisory lock when the holder exits for *any* reason — a clean finish, a crash, a kill, an OOM, a `systemctl stop`. A pidfile would go stale on a hard kill and wedge the schedule until someone cleared it by hand, which for an unattended timer is the one failure mode worth designing out.

The lock decision is pure enough to test with two file descriptors and no subprocess; running the agent is the half that needs one, kept behind `runner` so a test asserts the argv rather than launching a harness.
"""

import fcntl
import os
import signal
import subprocess
from pathlib import Path
from typing import Callable, Sequence

AgentRunner = Callable[[Sequence[str]], int]
"""How the guard runs the agent: argv in, exit code out. `run_agent` in production, a spy in tests."""


def guarded_collect(
    lock_path: Path,
    argv: Sequence[str],
    *,
    runner: AgentRunner,
    on_skip: Callable[[], None],
) -> int:
    """Run `argv` while holding an exclusive lock on `lock_path`, or skip if it is held.

    Args:
        lock_path: The lock file, created if missing. Its parent must already exist — the scheduler's `mkdir -p` guarantees it, since the lock shares `.steward/` with the log the redirect opens.
        argv: The agent command to run under the lock.
        runner: How to run it. Held for the command's whole lifetime, which is the point — the lock guards the agent, not merely the `steward collect` read inside it.
        on_skip: Called when the lock is already held, before returning. Where the caller records the skip.

    Returns:
        The agent's exit code, or `0` when the tick was skipped — a skip is not a failure, and a nonzero here would make the scheduler treat an ordinary busy interval as a fault.
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            on_skip()
            return 0
        return runner(argv)
    finally:
        # closing the fd drops the advisory lock; the `finally` is what makes a
        # runner that raises release it rather than hold it until process exit
        os.close(fd)


def run_agent(argv: Sequence[str]) -> int:
    """Run the agent to completion, forwarding termination so a stop reaches it.

    The scheduler holds *this* wrapper's pid, not the agent's, so a `systemctl stop`, a launchd unload, or a bare kill lands here. Forwarding `SIGTERM`/`SIGINT` to the child is what stops the agent with it rather than orphaning a harness that goes on spending tokens after the schedule was taken down. Output is inherited, so it flows to wherever the scheduler's shell redirect already points (`collect.log`).

    Args:
        argv: The agent command, with an absolute program path.

    Returns:
        The agent's exit code.
    """
    process = subprocess.Popen(list(argv))

    def forward(signum: int, _frame: object) -> None:
        process.send_signal(signum)

    previous = {
        sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        return process.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
