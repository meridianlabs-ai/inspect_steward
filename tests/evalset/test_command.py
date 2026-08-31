import os
import sys
import warnings
from pathlib import Path

import pytest
from inspect_steward._evalset.command import definition_command, warn_if_venv_declared
from inspect_steward._evalset.detect import DefinitionType

from ._hawk import requires_hawk

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
        # the pre-boundary artifacts and the pre-run log scan go here, not to
        # the run's directory
        "--log-dir",
        "/tmp/scratch",
        "--no-log-dir-create-unique",
        # both store halves are Steward's
        "--no-store-read",
        "--no-store-write",
        # in this interpreter, as hawk's --direct below: a per-worker venv
        # would run the eval in a grandchild Steward is not tracking
        "--set",
        "execution_type=inproc",
    ]


@requires_hawk
def test_command_hawk() -> None:
    path = FIXTURES / "hawk_config.yaml"
    command = definition_command(path, "hawk", log_dir="/tmp/scratch")
    assert command.argv == [
        sys.executable,
        "-m",
        "hawk",
        "local",
        "eval-set",
        str(path.resolve()),
        "--direct",
    ]
    # hawk has no log-dir option, so the scratch directory is not passed
    assert "/tmp/scratch" not in command.argv
    # hawk shells out to a bare `uv`, which lives beside the interpreter and is
    # otherwise only findable when the venv is activated
    assert command.env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)


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


@pytest.mark.parametrize(
    ("fixture", "type"),
    [("simple_evalset.py", "evalset"), ("hawk_config.yaml", "hawk")],
)
def test_command_args_require_flow(fixture: str, type: DefinitionType) -> None:
    with pytest.raises(ValueError, match="only supported for flow"):
        definition_command(FIXTURES / fixture, type, args={"x": 1})


@pytest.mark.parametrize(
    ("name", "content", "type", "expected"),
    [
        ("spec.yaml", "execution_type: venv\n", "flow", True),
        ("spec.yml", "execution_type: venv\n", "flow", True),
        ("spec.yaml", "execution_type: inproc\n", "flow", False),
        ("spec.yaml", "tasks: []\n", "flow", False),
        # the same declaration in a file the check does not read: a hawk config
        # has no such field, and a Python spec builds its FlowSpec in code
        ("config.yaml", "execution_type: venv\n", "hawk", False),
        ("spec.py", "execution_type = 'venv'\n", "flow", False),
        # neither a broken document nor a non-mapping is this check's to report
        ("spec.yaml", "tasks: [\n", "flow", False),
        ("spec.yaml", "[]\n", "flow", False),
    ],
)
def test_warn_if_venv_declared(
    name: str, content: str, type: DefinitionType, expected: bool, tmp_path: Path
) -> None:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        warn_if_venv_declared(path, type)
    assert bool(recorded) is expected
