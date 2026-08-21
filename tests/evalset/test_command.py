import importlib.util
import sys
from pathlib import Path

import pytest
from inspect_steward._evalset.command import definition_command

FIXTURES = Path(__file__).parent / "fixtures"


def test_command_evalset() -> None:
    path = FIXTURES / "simple_evalset.py"
    command = definition_command(path, "evalset")
    assert command.argv == [sys.executable, str(path.resolve())]
    assert command.cwd == str(Path.cwd().resolve())
    assert command.env == {}


def test_command_evalset_cwd_override(tmp_path: Path) -> None:
    command = definition_command(
        FIXTURES / "simple_evalset.py", "evalset", cwd=tmp_path
    )
    assert command.cwd == str(tmp_path.resolve())


def test_command_flow() -> None:
    path = FIXTURES / "flow_spec.py"
    command = definition_command(path, "flow", log_dir="/tmp/scratch")
    assert command.argv == [
        sys.executable,
        "-m",
        "inspect_flow._cli.main",
        "run",
        str(path.resolve()),
        "--log-dir",
        "/tmp/scratch",
    ]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"difficulty": "hard"}, ["-A", "difficulty=hard"]),
        ({"level": 2}, ["-A", "level=2"]),
        ({"ratio": 0.5}, ["-A", "ratio=0.5"]),
        ({"flag": True}, ["-A", "flag=true"]),
        ({"items": ["a", "b"]}, ["-A", "items=[a, b]"]),
        ({"a": 1, "b": "x"}, ["-A", "a=1", "-A", "b=x"]),
    ],
)
def test_command_flow_args(args: dict[str, object], expected: list[str]) -> None:
    command = definition_command(FIXTURES / "flow_spec.py", "flow", args=args)
    assert command.argv[-len(expected) :] == expected


def test_command_args_require_flow() -> None:
    with pytest.raises(ValueError, match="only supported for flow"):
        definition_command(FIXTURES / "simple_evalset.py", "evalset", args={"x": 1})


@pytest.mark.skipif(
    importlib.util.find_spec("hawk") is not None, reason="hawk is installed"
)
def test_command_hawk_requires_package() -> None:
    with pytest.raises(ValueError, match=r"inspect_steward\[hawk\]"):
        definition_command(FIXTURES / "hawk_config.yaml", "hawk")


@pytest.mark.skipif(
    importlib.util.find_spec("hawk") is None, reason="hawk not installed"
)
def test_command_hawk() -> None:
    path = FIXTURES / "hawk_config.yaml"
    command = definition_command(path, "hawk")
    assert command.argv == [
        sys.executable,
        "-m",
        "inspect_steward._runner.hawk",
        str(path.resolve()),
    ]
