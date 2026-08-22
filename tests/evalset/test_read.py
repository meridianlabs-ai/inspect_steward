from pathlib import Path

import pytest
from inspect_steward import Manifest, ReadEvalSetError, read_eval_set

FIXTURES = Path(__file__).parent / "fixtures"


def test_read_eval_set(tmp_path: Path) -> None:
    manifest = read_eval_set(FIXTURES / "simple_evalset.py", cwd=tmp_path)

    assert manifest.source.type == "evalset"
    assert manifest.source.content_hash.startswith("sha256:")
    assert manifest.source.args == {}
    assert manifest.options["log_dir"] == "logs"

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
