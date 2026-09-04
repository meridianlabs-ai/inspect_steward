"""Starting a marking run: what the tend's executor does about an unapplied `exclude` or `zero`.

Detached, like a worker, and for a reason beyond a zero's side run needing minutes: `recompute_metrics` resolves the log's metrics through `resolve_scorers_info`, which imports the task's module when a metric is not already registered, and a definition that calls `eval_set()` at module level would then run the eval inside the tend. The runner sets the capture guard before it touches anything of the eval's (`run.py`), and it does so in a process the tend does not have to wait for.
"""

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .._anomaly.model import Ruling
from .._evalset.classify import digest8
from .._workspace import Workspace
from .edit import Target
from .state import STEWARD_MARK, record_intent, record_launched

MODULE = "inspect_steward"

MARK_ATTEMPTS = 3
"""Runs a ruling may spend without landing before the executor stops starting them and reports it.

Bounded the way a task's respawns are: a runner that keeps failing — the claim never frees, the side run never lands, the log will not write — is a defect to look at, and starting it every turn for the life of the run would hide that behind a log nobody reads. Three, because the ordinary failure is a transient a second try clears, and a third is the operator's to read.
"""


def run_id(class_key: str, ruling_ts: str, attempt: int) -> str:
    """The run's id: the ruling's digest and which try at it this is.

    The attempt number keeps a retry from folding onto the run it retries — the record keys on the id, and so does the scratch directory a zero's side run writes into.
    """
    return f"{digest8(f'{class_key}:{ruling_ts}')}-{attempt}"


def spawn_runner(
    workspace: Workspace,
    class_key: str,
    ruling: Ruling,
    targets: Sequence[Target],
    *,
    attempt: int,
) -> str:
    """Start a detached runner for one ruling's remainder.

    Intent before `Popen`, as the fleet spawns (`_worker.spawn`): a crash between the two leaves a run whose existence nothing else would know about. The runner writes its own `exited`.

    Args:
        workspace: The workspace the ruling stands in.
        class_key: The ruled class.
        ruling: The ruling — its instant is the record's key, its disposition the work.
        targets: The ruled samples not yet written, addressed to their current logs.
        attempt: Which try at this ruling this is, from the runs record.

    Returns:
        The run id.
    """
    run = run_id(class_key, ruling.ts, attempt)
    directory = workspace.marks_run(run)
    directory.mkdir(parents=True, exist_ok=True)
    # the running interpreter, absolutely, as the timer spawns: a scheduled
    # tend inherits almost no PATH
    argv = [sys.executable, "-m", MODULE, "_mark", "--run", run]
    record_intent(
        workspace.marks_runs,
        run=run,
        class_key=class_key,
        ruling_ts=ruling.ts,
        disposition=ruling.disposition,
        targets=targets,
        argv=argv,
    )
    pid = _launch(
        argv,
        cwd=workspace.root,
        env={**os.environ, STEWARD_MARK: run, "INSPECT_DISPLAY": "plain"},
        output=directory / "run.log",
    )
    record_launched(workspace.marks_runs, run=run, pid=pid)
    return run


def _launch(argv: list[str], *, cwd: Path, env: dict[str, str], output: Path) -> int:
    """Start the process, detached, and return its pid.

    Its own function so the suite can stand in for it: a test that rules an exclusion must not start a real process editing its fixture from the background, and applies the run in-process instead (`tests/marks/_runner.py`).
    """
    with output.open("ab") as stream:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return process.pid


__all__ = ["MARK_ATTEMPTS", "MODULE", "run_id", "spawn_runner"]
