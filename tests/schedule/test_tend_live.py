"""The loop, closed once, on a real eval.

Everything else about the turn is settled in `test_tend.py` against synthesized
state. What no synthesized state can show is that the four layers agree with
each other about the *same* run: capture writes identifiers and a content hash,
a worker writes a log, and observation matches the two. Each layer is otherwise
tested against a fixture of the next one's shape, and a fixture is a belief
about that shape rather than the shape itself.

**Budget: three launches** (plan.md §10) — one capture and two workers, in one
test. Two things the plan sketched are deliberately not here. The no-log stall
is not repeated: `test_tend.py` runs that path with real processes, a real
in-flight record, and the real guard, and paying an eval's startup for it would
buy only the knowledge that `faulty_evalset.py`'s `pre:crash` lands no log,
which `tests/worker/test_faults.py` already establishes. Drift against a real
capture is not its own test either — it is one assertion below, because a
capture is exactly what makes `drift is False` mean anything.
"""

import shutil
from pathlib import Path

from inspect_ai.log import list_eval_logs
from inspect_steward import read_eval_set
from inspect_steward._evalset.manifest import write_manifest
from inspect_steward._workspace import Workspace

from .test_tend import settle, turn

FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"


def test_a_run_converges_and_then_stays_converged(tmp_path: Path) -> None:
    workspace = Workspace.at(tmp_path)
    workspace.root.mkdir(parents=True, exist_ok=True)
    definition = workspace.root / "evalset.py"
    shutil.copy(FIXTURES / "simple_evalset.py", definition)
    manifest = read_eval_set(definition, cwd=workspace.root)
    write_manifest(manifest, workspace.manifest)

    started = turn(workspace)
    settle(workspace)
    finished = turn(workspace)
    again = turn(workspace)

    # capture and drift hash the same file the same way, which is a claim about
    # two modules agreeing and cannot be made against a synthesized manifest
    assert started.drift is False
    assert len(started.spawned) == len(manifest.tasks) == 2

    # the identifiers capture wrote are the ones observation recovered from the
    # logs those workers landed -- the correlation the whole design rests on
    assert sorted(finished.reaped) == sorted(started.spawned)
    assert finished.summary.states["complete"] == 2
    assert finished.spawned == []

    # and a converged run is a fixed point rather than somewhere it passes
    # through: the second tend does nothing, and so would the two hundredth
    assert (again.spawned, again.reaped, again.archived) == ([], [], [])
    assert again.summary.states["complete"] == 2
    assert len(list_eval_logs(str(workspace.logs))) == 2
