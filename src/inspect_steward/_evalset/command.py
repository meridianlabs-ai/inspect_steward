import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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
    log_dir: str | None = None,
) -> DefinitionCommand:
    """Build the command that executes an eval set definition.

    Args:
        path: Path to the definition file.
        type: Definition type.
        args: Arguments for the definition (flow spec function args only).
        cwd: Working directory for the command (defaults to the current working directory, matching how the definition would run by hand).
        log_dir: Log directory override (flow definitions only; other definition types carry their own log directory). Reads point this at a scratch directory so the definition's real log directory is left untouched.

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
    else:
        _require_package("inspect_flow", "flow")
        # flow's own CLI is a conforming program: it culminates in the
        # eval_set() call this definition describes (module form rather than
        # the `flow` console script so we stay in the current interpreter)
        argv = [sys.executable, "-m", "inspect_flow._cli.main", "run", abs_path]
        if log_dir is not None:
            argv += ["--log-dir", log_dir]
        argv += _arg_options(args)

    return DefinitionCommand(argv=argv, cwd=str((cwd or Path.cwd()).resolve()))


def _arg_options(args: dict[str, Any] | None) -> list[str]:
    """Render definition args as flow `-A KEY=VALUE` options.

    Values are YAML-encoded to survive the round trip through flow's arg parsing (inspect_ai's `parse_cli_args`). Note that parser splits bare strings on commas, so a string value containing a comma arrives as a list.
    """
    options: list[str] = []
    for key, value in (args or {}).items():
        encoded = yaml.safe_dump(value, default_flow_style=True).strip()
        # safe_dump terminates plain scalar documents with '...'
        encoded = encoded.removesuffix("\n...").strip()
        options += ["-A", f"{key}={encoded}"]
    return options


def _require_package(package: str, extra: str) -> None:
    if importlib.util.find_spec(package) is None:
        raise ValueError(
            f"The '{package}' package is required to run this definition. "
            f"Install it with: pip install inspect_steward[{extra}]"
        )
