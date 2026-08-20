import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .detect import DefinitionType


@dataclass(frozen=True)
class DefinitionCommand:
    """A command that executes an eval set definition.

    The command is mode-agnostic: run as-is it executes the definition normally; with `INSPECT_EVAL_SET_CAPTURE` set it enumerates; with a future selection environment it will run a subset of tasks.
    """

    argv: list[str]
    """Command arguments."""

    cwd: str
    """Working directory (absolute)."""

    env: dict[str, str] = field(default_factory=dict[str, str])
    """Environment additions (merged over the ambient environment)."""


def definition_command(
    path: Path,
    type: DefinitionType,
    args: dict[str, Any] | None = None,
    cwd: Path | None = None,
) -> DefinitionCommand:
    """Build the command that executes an eval set definition.

    Args:
        path: Path to the definition file.
        type: Definition type.
        args: Arguments for the definition (flow spec function args only).
        cwd: Working directory for the command (defaults to the current working directory, matching how the definition would run by hand).

    Returns:
        Command to execute the definition.

    Raises:
        ValueError: If `args` are passed for a non-flow definition, or the package required to run the definition type is not installed.
    """
    if args and type != "flow":
        raise ValueError(
            f"Definition args are only supported for flow definitions (got type '{type}')."
        )

    abs_path = str(path.resolve())
    if type == "evalset":
        argv = [sys.executable, abs_path]
    elif type == "flow":
        _require_package("inspect_flow", "flow")
        argv = [
            sys.executable,
            "-m",
            "inspect_steward._runner.flow",
            abs_path,
            "--args",
            json.dumps(args or {}),
        ]
    else:
        _require_package("hawk", "hawk")
        argv = [sys.executable, "-m", "inspect_steward._runner.hawk", abs_path]

    return DefinitionCommand(argv=argv, cwd=str((cwd or Path.cwd()).resolve()))


def _require_package(package: str, extra: str) -> None:
    if importlib.util.find_spec(package) is None:
        raise ValueError(
            f"The '{package}' package is required to run this definition. "
            f"Install it with: pip install inspect_steward[{extra}]"
        )
