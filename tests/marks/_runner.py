"""Applying marking runs in-process, where a test can see them land.

The suite stands in for the runner's launch (`tests/conftest.py`), so a turn that rules an exclusion records a run and starts nothing. This is the other half: every recorded run that has not ended is carried out here, in this process, with the runner's own body — so the effect is the production one and the timing is the test's.
"""

import os

from inspect_ai._eval.eval_set_manifest import INSPECT_EVAL_SET_CAPTURE
from inspect_steward._marks.run import run_mark
from inspect_steward._marks.state import read_runs
from inspect_steward._workspace import Workspace


def apply_marks(workspace: Workspace) -> list[str]:
    """Carry out every recorded run that has not ended, and return their ids.

    Asserts each one succeeded: a test applying a mark wants the effect, and a run that failed says why in the record.
    """
    guard = os.environ.get(INSPECT_EVAL_SET_CAPTURE)
    applied: list[str] = []
    try:
        for run in list(read_runs(workspace.marks_runs).values()):
            if run.exited:
                continue
            status = run_mark(workspace, run.run)
            ended = read_runs(workspace.marks_runs)[run.run]
            assert status == 0, f"run {run.run} failed: {ended.detail}"
            applied.append(run.run)
    finally:
        # the runner sets the capture guard in its own environment, which in
        # this process is the test's
        if guard is None:
            os.environ.pop(INSPECT_EVAL_SET_CAPTURE, None)
        else:
            os.environ[INSPECT_EVAL_SET_CAPTURE] = guard
    return applied
