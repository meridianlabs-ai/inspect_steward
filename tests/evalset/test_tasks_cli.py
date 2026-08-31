from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._cli.tasks import parse_args
from inspect_steward._evalset.manifest import Manifest

FIXTURES = Path(__file__).parent / "fixtures"


def test_cli_tasks() -> None:
    result = CliRunner().invoke(steward, ["tasks", str(FIXTURES / "simple_evalset.py")])
    assert result.exit_code == 0, result.output
    assert "addition[generate]@mockllm/model" in result.output
    assert "2 tasks, 4 total samples" in result.output


def test_cli_tasks_json() -> None:
    result = CliRunner().invoke(
        steward, ["tasks", str(FIXTURES / "simple_evalset.py"), "--json"]
    )
    assert result.exit_code == 0, result.output
    manifest = Manifest.model_validate_json(result.output)
    assert len(manifest.tasks) == 2


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("difficulty=hard", {"difficulty": "hard"}),
        ("level=2", {"level": 2}),
        ("ratio=0.5", {"ratio": 0.5}),
        ("flag=true", {"flag": True}),
        ("items=a,b,c", {"items": ["a", "b", "c"]}),
        ("nums=[1, 2]", {"nums": [1, 2]}),
        # dashes in keys become underscores (matches inspect_ai parse_cli_args)
        ("max-samples=5", {"max_samples": 5}),
    ],
)
def test_cliparse_args(arg: str, expected: dict[str, Any]) -> None:
    assert parse_args((arg,)) == expected


def test_cli_tasks_flow_args() -> None:
    result = CliRunner().invoke(
        steward,
        [
            "tasks",
            str(FIXTURES / "flow_args_spec.py"),
            "-A",
            "difficulty=hard",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    manifest = Manifest.model_validate_json(result.output)
    assert manifest.source.args == {"difficulty": "hard"}
    assert manifest.tasks[0].args == {"difficulty": "hard"}


def test_cli_tasks_args_require_flow() -> None:
    result = CliRunner().invoke(
        steward,
        ["tasks", str(FIXTURES / "simple_evalset.py"), "-A", "level=2"],
    )
    assert result.exit_code != 0
    assert "only supported for flow" in result.output


def test_cli_tasks_definition_error() -> None:
    result = CliRunner().invoke(steward, ["tasks", str(FIXTURES / "raises_early.py")])
    assert result.exit_code != 0
    assert "failed before reaching eval_set" in result.output
