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
"""

from pathlib import Path

import pytest
from inspect_ai._eval.evalset import task_identifier
from inspect_ai.log import EvalLog, list_eval_logs
from inspect_steward import Manifest, read_eval_set

from ._hawk import requires_hawk
from ._worker import DEFAULT_EVAL_SET_ID, landed_logs, run_workers, selection

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


def test_identifier_correlates_across_concurrent_workers(tmp_path: Path) -> None:
    # the production shape: one task per worker, all of them writing into one
    # flat directory at the same time. Four workers cost the same wall time as
    # one, so this also carries the working-directory case: a task's source file
    # is part of its identity and inspect warns that a worker running from
    # elsewhere may not match, but Steward is immune by construction --
    # `definition_command` resolves the definition absolutely. If that stops
    # being true, correlation breaks silently and this fails.
    definition = FIXTURES / "sweep_evalset.py"
    manifest = read_eval_set(definition, cwd=tmp_path)
    logs = tmp_path / "logs"
    other = tmp_path / "elsewhere"
    other.mkdir()

    results = run_workers(
        definition,
        [selection([task.identifier], logs) for task in manifest.tasks],
        cwd=other,
    )
    assert all(result.ok for result in results), [r.stdout for r in results if not r.ok]

    landed = landed_logs(logs)
    assert_correlates(manifest, landed)
    # the runner owns the eval set id; workers stamp what they are told
    assert {log.eval.eval_set_id for log in landed} == {DEFAULT_EVAL_SET_ID}


def test_identifier_correlates_on_resume(tmp_path: Path) -> None:
    definition = FIXTURES / "simple_evalset.py"
    manifest = read_eval_set(definition, cwd=tmp_path)
    identifier = manifest.tasks[0].identifier
    logs = tmp_path / "logs"

    first = run_workers(definition, [selection([identifier], logs)], cwd=tmp_path)
    assert first[0].ok, first[0].stdout
    prior = list_eval_logs(str(logs))[0].name

    resumed = run_workers(
        definition,
        [selection([identifier], logs, resume={identifier: prior})],
        cwd=tmp_path,
    )
    assert resumed[0].ok, resumed[0].stdout

    # both attempts correlate to the one task -- which is also the supersession
    # case: two logs for one identifier in a shared directory
    landed = landed_logs(logs)
    assert len(landed) == 2
    assert {task_identifier(log, None) for log in landed} == {identifier}

    # a resume naming another task's log is rejected by inspect itself, in the
    # same code path this test exercises. Not re-tested here: it is upstream's
    # behaviour and upstream's to cover, and asserting it costs two more workers.
