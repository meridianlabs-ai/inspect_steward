"""Waiting on conditions, and breaking a worker at a chosen point.

testing.md §4 has two rules that keep a fault suite from becoming flaky, and both are about time: *inject at decision points, never at wall-clock times*, and *no `sleep`*. Everything here exists so that obeying them is the easy path — `until` instead of a number, and a fault fixture's own markers instead of an estimate of how long its worker takes to get somewhere.

`until` had drifted into three copies before this module existed, which is what says it belongs in one.

Not named `test_*`, so pytest does not collect it.
"""

import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from inspect_steward._worker import SpawnedWorker

FAULT_FIXTURE = "faulty_evalset.py"
"""The definition `arm` drives. Named here so a test says which fault it wants, not where the file is."""


def until(what: str, predicate: Callable[[], bool], timeout: float = 120) -> None:
    """Poll until something is true, or say what never became true.

    Args:
        what: What is being waited for, phrased to complete "timed out waiting for ...".
        predicate: Checked repeatedly.
        timeout: Seconds to keep checking. Generous rather than tuned — a stuck-test backstop, not a schedule, and every caller should finish in far less.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


@dataclass(frozen=True)
class Fault:
    """An armed fault, and the two markers that drive it."""

    dir: Path
    """Where the markers live — outside the workspace, because deleting `.steward/` is one of the faults."""

    point: str
    """`pre`, `run`, or `post`. One per run, so nothing here has to be told which."""

    def reached(self, *, timeout: float = 120) -> None:
        """Wait until the worker announces it has arrived."""
        marker = self.dir / f"{self.point}.reached"
        until(f"the worker to reach {self.point}", marker.exists, timeout)

    def release(self) -> None:
        """Let a worker held at the point carry on."""
        (self.dir / f"{self.point}.go").touch()


def arm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str) -> Fault:
    """Arm `faulty_evalset.py` for every worker spawned from this test.

    Through the environment rather than an argument, because a worker inherits it and a definition cannot be passed anything — the same reason inspect marks worker mode that way.

    Args:
        monkeypatch: Pytest's environment patcher.
        tmp_path: The test's temp directory. The marker directory is a sibling of the workspace, not a child.
        fault: `<point>:<behaviour>`, e.g. `run:hang`, `pre:crash`, `post:exit:3`.

    Returns:
        The armed fault.
    """
    directory = tmp_path / "fault"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FAULTY_EVALSET_FAULT", fault)
    monkeypatch.setenv("FAULTY_EVALSET_DIR", str(directory))
    return Fault(dir=directory, point=fault.partition(":")[0])


def kill(worker: SpawnedWorker) -> None:
    """Kill a worker and reap it, so its pid is not still claimed by a zombie."""
    if worker.process.poll() is None:
        os.kill(worker.pid, signal.SIGKILL)
    worker.process.wait(timeout=60)
