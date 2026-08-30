from pathlib import Path
from typing import Any

import pytest
from inspect_ai._eval.evalset import TASK_IDENTIFIER_VERSION
from inspect_steward import Manifest, ReadEvalSetError, read_eval_set
from inspect_steward._evalset.manifest import MANIFEST_VERSION
from inspect_steward._schedule import Pool, resolve_samples_ramp

from ._hawk import requires_hawk

FIXTURES = Path(__file__).parent / "fixtures"


def test_read_eval_set(tmp_path: Path) -> None:
    manifest = read_eval_set(FIXTURES / "simple_evalset.py", cwd=tmp_path)

    assert manifest.source.type == "evalset"
    # a manifest outlives the inspect that produced it, so it has to say which
    # task_identifier computation its identifiers came from
    assert manifest.identifier_version == TASK_IDENTIFIER_VERSION
    assert manifest.source.content_hash.startswith("sha256:")
    assert manifest.source.args == {}
    assert manifest.options["log_dir"] == "logs"
    # present but unset: `resolve_max_samples` yields to a definition that asked
    # for a value, and only a key that is always there makes "asked for nothing"
    # distinguishable from an older capture that could not say
    assert manifest.options["max_samples"] is None

    assert [task.name for task in manifest.tasks] == ["addition", "echo"]
    addition, echo = manifest.tasks
    assert addition.key == "addition[generate]@mockllm/model"
    assert addition.samples == 2
    assert addition.epochs == 1
    assert echo.samples == 1
    assert echo.epochs == 2
    assert all(task.identifier for task in manifest.tasks)

    # capture never touches the log directory
    assert not (tmp_path / "logs").exists()


def test_a_definition_that_names_no_log_dir_is_captured_as_saying_nothing(
    tmp_path: Path,
) -> None:
    """The hinge of the whole resolution: *silent* has to be distinguishable from *chose ./logs*.

    Capture builds its options above the point `eval_set()` resolves the
    default, so a definition that named none is reported as having named none —
    which is what lets Steward answer with the workspace's `logs/` or with a
    directory under the machine's root, rather than with the process's cwd.
    """
    manifest = read_eval_set(FIXTURES / "no_log_dir_evalset.py", cwd=tmp_path)

    assert manifest.options["log_dir"] is None
    assert not (tmp_path / "logs").exists()


def test_read_eval_set_sweep(tmp_path: Path) -> None:
    manifest = read_eval_set(FIXTURES / "sweep_evalset.py", cwd=tmp_path)

    # two args crossed over two models
    assert len(manifest.tasks) == 4
    assert len({task.identifier for task in manifest.tasks}) == 4
    keys = {task.key for task in manifest.tasks}
    assert keys == {
        "sweep[generate]@mockllm/model (difficulty=easy)",
        "sweep[generate]@mockllm/model (difficulty=hard)",
        "sweep[generate]@mockllm/model2 (difficulty=easy)",
        "sweep[generate]@mockllm/model2 (difficulty=hard)",
    }


def test_read_eval_set_json_round_trip(tmp_path: Path) -> None:
    manifest = read_eval_set(FIXTURES / "simple_evalset.py", cwd=tmp_path)
    restored = Manifest.model_validate_json(manifest.model_dump_json())
    assert restored == manifest


def test_a_capture_measures_what_reading_the_definition_cost(tmp_path: Path) -> None:
    """The figure a wide run is judged against, taken while it is already being paid.

    Capture constructs every task in the eval set and a worker constructs only
    its own, so this bounds a worker's startup rather than estimating it —
    measured, either way, rather than guessed at. Asserted as a range rather
    than a number: the claim is *a real process was watched*, and a Python
    interpreter that imported inspect_ai is comfortably inside it.
    """
    manifest = read_eval_set(FIXTURES / "simple_evalset.py", cwd=tmp_path)

    assert manifest.source.capture_rss is not None
    assert 10_000_000 < manifest.source.capture_rss < 20_000_000_000


def test_a_manifest_written_before_the_measurement_still_reads() -> None:
    """The reason `MANIFEST_VERSION` did not move for `capture_rss`.

    Bumping would have made every committed manifest unreadable — a mid-run
    re-capture to gain a field nothing requires. The version gate is for a
    schema the reader would have to guess at, and a field whose absence means
    *not measured* is not one.
    """
    document: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "identifier_version": TASK_IDENTIFIER_VERSION,
        "source": {
            "type": "evalset",
            "path": "evalset.py",
            "content_hash": "sha256:abc",
            "args": {},
        },
        "options": {},
        "tasks": [],
    }

    manifest = Manifest.model_validate(document)

    assert manifest.source.capture_rss is None


@pytest.mark.parametrize("fixture", ["flow_spec.py", "flow_spec.yaml"])
def test_read_eval_set_flow(fixture: str, tmp_path: Path) -> None:
    manifest = read_eval_set(FIXTURES / fixture, cwd=tmp_path)

    assert manifest.source.type == "flow"
    assert len(manifest.tasks) == 1
    task = manifest.tasks[0]
    # flow names file tasks with the full file@task spec string
    assert task.name == "tasks.py@addition"
    assert task.registry_name == "addition"
    assert task.model == "mockllm/model"
    assert task.samples == 2
    assert task.identifier
    assert not (tmp_path / "logs").exists()
    assert not (FIXTURES / "logs").exists()


@requires_hawk
@pytest.mark.network
def test_read_eval_set_hawk(tmp_path: Path) -> None:
    # Unlike every other definition type, reading a hawk config is not
    # hermetic: `hawk local eval-set --direct` runs `uv pip install` into the
    # interpreter running this test, resolving inspect-ai from a pinned git
    # commit. It is a no-op only while this environment happens to satisfy
    # hawk's self-pin -- an accident of current state, not an invariant. Hence
    # the marker; see the `network` entry in pyproject.toml.
    manifest = read_eval_set(FIXTURES / "hawk_config.yaml", cwd=tmp_path)

    assert manifest.source.type == "hawk"

    # the manifest is hawk's own lowering: one task crossed over two solvers,
    # which is the crossing Steward deliberately does not re-derive
    assert len(manifest.tasks) == 2
    assert {task.solver for task in manifest.tasks} == {"generate", "chain_of_thought"}
    assert {task.name for task in manifest.tasks} == {"hawk/hawk/e2e_hello"}
    assert all(task.model == "mockllm/model" for task in manifest.tasks)
    assert len({task.identifier for task in manifest.tasks}) == 2

    # hawk synthesizes a local infra config whose log directory is relative to
    # the working directory; capture must exit before anything creates it
    assert manifest.options["log_dir"].startswith("logs/")
    assert not (tmp_path / "logs").exists()
    assert not (FIXTURES / "logs").exists()

    # hawk's infra config supplies a max_samples of its own (1000, and this
    # fixture sets none), which Steward reads as the definition expressing a
    # setpoint -- so a hawk run is pinned and never ramps. Asserted as the
    # property rather than the number, because the number is hawk's to change
    # and only its *presence* is what the documentation promises on
    assert resolve_samples_ramp(manifest, Pool()) is None


def test_read_eval_set_flow_args(tmp_path: Path) -> None:
    manifest = read_eval_set(
        FIXTURES / "flow_args_spec.py", args={"difficulty": "hard"}, cwd=tmp_path
    )

    assert manifest.source.args == {"difficulty": "hard"}
    task = manifest.tasks[0]
    assert task.name == "tasks.py@sweep"
    assert task.args == {"difficulty": "hard"}


def test_read_eval_set_env_cannot_break_capture(tmp_path: Path) -> None:
    # a caller-supplied capture var must not displace the protocol's own
    bogus = tmp_path / "bogus" / "manifest.json"
    manifest = read_eval_set(
        FIXTURES / "simple_evalset.py",
        cwd=tmp_path,
        env={"INSPECT_EVAL_SET_CAPTURE": str(bogus)},
    )
    assert len(manifest.tasks) == 2
    assert not bogus.exists()


def test_read_eval_set_timeout(tmp_path: Path) -> None:
    with pytest.raises(ReadEvalSetError, match="Timed out"):
        read_eval_set(FIXTURES / "slow_evalset.py", cwd=tmp_path, timeout=3)


def test_read_eval_set_never_called(tmp_path: Path) -> None:
    with pytest.raises(ReadEvalSetError, match="never called eval_set"):
        read_eval_set(FIXTURES / "no_eval_set.py", cwd=tmp_path)


def test_read_eval_set_definition_error(tmp_path: Path) -> None:
    with pytest.raises(ReadEvalSetError, match="failed during setup"):
        read_eval_set(FIXTURES / "raises_early.py", cwd=tmp_path)


def test_read_eval_set_missing_file() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        read_eval_set("nonexistent.py")
