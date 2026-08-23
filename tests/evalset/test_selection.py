"""A landed log maps back to the manifest task that produced it.

Every scheduling decision Steward makes rests on this, and it is not obviously
true: `task_identifier` computes its string from a `ResolvedTask` before a run
and from an `EvalLog` after, and the two branches read every field from a
different place. Workers resolve independently of the process that enumerated
them, which is the same asymmetry that produced the `eval-set.json` `task_id`
trap.

Selection mode already proves half of it for free — a worker matches its
selection by recomputing identifiers from *its own* resolved tasks, so a
selection that runs at all has shown capture and resolution agree across
processes. The half nothing exercises is resolution against the **log**, and
that is the half Steward reads. Upstream touches it in one place, when
validating a resume target; a run that never resumes never touches it at all.

These tests are the guard, and they are tests rather than a one-off
verification because the property has to keep holding across inspect upgrades.

The *production* shape — one task per worker, spawned detached — is covered in
`tests/worker/test_spawn.py`, which exercises the real spawn. What is left here
is the case production never has: one worker running a whole manifest, which is
what keeps a fifteen-task fixture affordable.
"""

from pathlib import Path

import pytest
from inspect_ai._eval.evalset import task_identifier
from inspect_ai.log import EvalLog
from inspect_steward import Manifest, read_eval_set

from ._hawk import requires_hawk
from ._worker import landed_logs, run_workers, selection

FIXTURES = Path(__file__).parent / "fixtures"


def run_whole_definition(
    fixture: str, work: Path, *, cwd: Path | None = None
) -> tuple[Manifest, list[EvalLog]]:
    """Capture a definition, run every task it resolves, and read what landed.

    One worker runs the whole manifest, which the protocol allows and which
    keeps a fifteen-task fixture cheap. The one-task-per-worker shape production
    uses is covered separately.

    Args:
        fixture: Definition file name under `fixtures/`.
        work: Working directory (also the parent of the log directory).
        cwd: Working directory for the *worker*, when it should differ from the one the definition was captured in.

    Returns:
        The capture manifest and the logs the run produced.
    """
    definition = FIXTURES / fixture
    manifest = read_eval_set(definition, cwd=work)
    logs = work / "logs"
    results = run_workers(
        definition,
        [selection([task.identifier for task in manifest.tasks], logs)],
        cwd=cwd or work,
    )
    assert results[0].ok, results[0].stdout + results[0].stderr
    return manifest, landed_logs(logs)


def assert_correlates(manifest: Manifest, logs: list[EvalLog]) -> None:
    """Assert every landed log recomputes to the identifier of the task that produced it."""
    identifiers = {task.identifier for task in manifest.tasks}
    # a dimension that dropped out of the hash would collide rather than fail,
    # so uniqueness is what keeps the set comparison below from passing vacuously
    assert len(identifiers) == len(manifest.tasks), "manifest identifiers collided"
    assert len(logs) == len(manifest.tasks)
    assert {task_identifier(log, None) for log in logs} == identifiers


@pytest.mark.parametrize("fixture", ["identity_evalset.py", "identity_set_evalset.py"])
def test_identifier_correlates_across_identity_dimensions(
    fixture: str, tmp_path: Path
) -> None:
    # every task in these fixtures shares a name and args and differs in one
    # identity-relevant field, so this covers each field the two branches of
    # task_identifier read from different places
    manifest, logs = run_whole_definition(fixture, tmp_path)
    assert_correlates(manifest, logs)


@pytest.mark.parametrize(
    "fixture",
    [
        "flow_spec.py",
        pytest.param("hawk_config.yaml", marks=[requires_hawk, pytest.mark.network]),
    ],
)
def test_identifier_correlates_by_definition_type(fixture: str, tmp_path: Path) -> None:
    # only the frontends: plain `eval_set()` definitions are covered by every
    # other test in this file, and a test here costs two interpreter startups
    other = tmp_path / "elsewhere"
    other.mkdir()
    manifest, logs = run_whole_definition(fixture, tmp_path, cwd=other)
    assert_correlates(manifest, logs)
