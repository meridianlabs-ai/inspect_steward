"""A frontend's once-per-run work stays out of the run's log directory.

Flow resolves its spec, writes `flow.yaml` and a requirements snapshot, and
scans for prior logs — all of it *before* `eval_set()`, where a selection's
override cannot yet reach, and all of it once-per-run work that every worker
repeats. Pointed at the shared log directory that is N concurrent writes to two
paths plus a scan whose cost grows with the run; pointed at each worker's own
scratch directory it is neither.

Flow is the frontend where this is checkable offline. Hawk has the same shape,
no `--log-dir` to redirect, and a network dependency — see hawk.md §6.
"""

from pathlib import Path

from inspect_steward._evalset.detect import DefinitionType

from ._fleet import fan_out, landed

FRONTEND_ARTIFACTS = ("flow.yaml", "flow-requirements.txt")


def test_a_flow_fan_out_leaves_the_log_directory_to_the_logs(tmp_path: Path) -> None:
    type: DefinitionType = "flow"
    manifest, workers, spawned = fan_out("flow_sweep.py", tmp_path, type=type)
    logs = Path(workers.log_dir)

    # the eval still lands where the selection said, under the right identifier
    assert len(spawned) == 2
    assert sorted(landed(logs)) == sorted(task.identifier for task in manifest.tasks)

    # ...and nothing else did
    for artifact in FRONTEND_ARTIFACTS:
        assert not (logs / artifact).exists()

    # each worker's pre-boundary work went to a directory of its own, so no two
    # of them wrote the same path
    scratches = {worker.scratch for worker in spawned}
    assert len(scratches) == len(spawned)
    for scratch in scratches:
        # flow.yaml only: the requirements snapshot beside it is written on a
        # best-effort basis (flow warns and carries on if uv fails), so its
        # absence would not mean the redirect stopped working
        assert (scratch / "flow.yaml").exists()
