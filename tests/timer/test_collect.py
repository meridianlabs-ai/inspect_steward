"""The overlap lock a scheduled collect runs under.

The one thing that is only true here: a second collect started while the first is
still running is skipped, not stacked — and the lock a crash leaves behind is
released by the kernel rather than left to wedge the schedule. Both are driven
with real file descriptors and no subprocess, since that is exactly the half of
the mechanism a test can hold two ends of.
"""

import fcntl
import os
from pathlib import Path
from typing import Sequence

from inspect_steward._timer import guarded_collect, run_agent


def test_a_free_lock_runs_the_agent_and_returns_its_code(tmp_path: Path) -> None:
    seen: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> int:
        seen.append(argv)
        return 7

    code = guarded_collect(
        tmp_path / "collect.lock",
        ["codex", "exec"],
        runner=runner,
        on_skip=lambda: seen.append(["skipped"]),
    )

    assert code == 7
    assert seen == [["codex", "exec"]]


def test_a_held_lock_skips_without_running_the_agent(tmp_path: Path) -> None:
    lock = tmp_path / "collect.lock"
    # a second file description on the same file contends even within one
    # process, which is what lets this hold the lock the guard then tries for
    held = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        ran = False
        skipped = False

        def runner(_argv: Sequence[str]) -> int:
            nonlocal ran
            ran = True
            return 0

        def on_skip() -> None:
            nonlocal skipped
            skipped = True

        code = guarded_collect(lock, ["codex"], runner=runner, on_skip=on_skip)

        # a skip is not a failure: 0 so the scheduler does not read a busy
        # interval as a fault, the agent never started, the skip was recorded
        assert code == 0
        assert not ran
        assert skipped
    finally:
        os.close(held)


def test_the_lock_is_released_for_the_next_run(tmp_path: Path) -> None:
    lock = tmp_path / "collect.lock"
    runs = 0

    def runner(_argv: Sequence[str]) -> int:
        nonlocal runs
        runs += 1
        return 0

    for _ in range(3):
        guarded_collect(lock, ["codex"], runner=runner, on_skip=lambda: None)

    # each run released the lock on exit, so none skipped the next
    assert runs == 3


def test_a_runner_that_raises_still_releases_the_lock(tmp_path: Path) -> None:
    lock = tmp_path / "collect.lock"

    def boom(_argv: Sequence[str]) -> int:
        raise RuntimeError("agent died")

    try:
        guarded_collect(lock, ["codex"], runner=boom, on_skip=lambda: None)
    except RuntimeError:
        pass

    # the lock is free again: a fresh guard runs rather than reading the wedged
    # lock of the crashed one
    ran = False

    def runner(_argv: Sequence[str]) -> int:
        nonlocal ran
        ran = True
        return 0

    guarded_collect(lock, ["codex"], runner=runner, on_skip=lambda: None)
    assert ran


def test_run_agent_returns_the_child_exit_code() -> None:
    assert run_agent(["sh", "-c", "exit 5"]) == 5
    assert run_agent(["sh", "-c", "exit 0"]) == 0
