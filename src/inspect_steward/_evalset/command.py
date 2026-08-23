import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .detect import DefinitionType, install_hint


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
    env: dict[str, str] = {}
    if type == "evalset":
        argv = [sys.executable, abs_path]
    elif type == "flow":
        _require_package("inspect_flow", "flow")
        # flow's own CLI is a conforming program: it culminates in the
        # eval_set() call this definition describes (module form rather than
        # the `flow` console script so we stay in the current interpreter)
        argv = [sys.executable, "-m", "inspect_flow._cli.main", "run", abs_path]
        if log_dir is not None:
            argv += ["--log-dir", log_dir]
        argv += _arg_options(args)
    else:
        _require_package("hawk", "hawk")
        # hawk's CLI is a conforming program for the same reason, and
        # `local eval-set` is the command a user runs by hand. Driving it
        # rather than hawk's runner module keeps hawk's pre-boundary work --
        # secrets resolution, provider env for middleman routing, rejecting
        # `scan:` -- where it belongs.
        #
        # --direct runs in this interpreter; without it hawk builds a fresh
        # venv per worker. Note it does not mean "skip installing": hawk still
        # runs `uv pip install` into the *current* environment on every
        # invocation (`run_in_venv.install_into_current`). That is a no-op in a
        # consistent environment, because hawk pins what is already installed,
        # but a config declaring `packages:` installs them into the caller's
        # venv, and N workers starting together run N concurrent installs.
        #
        # log_dir is deliberately not passed: hawk has no such option, and a
        # local run's log directory comes from the infra config it synthesizes
        # for itself. That costs nothing here because capture exits before the
        # directory is used, and workers override it through the selection.
        argv = [sys.executable, "-m", "hawk", "local", "eval-set", abs_path, "--direct"]
        # that install shells out to a bare `uv`, resolved through PATH. We
        # declare uv in the [hawk] extra, but pip puts it beside the
        # interpreter -- a directory that is only on PATH when the venv happens
        # to be activated, so `.venv/bin/steward` would still die with
        # FileNotFoundError. We chose this interpreter; making its uv reachable
        # is ours to do. An ambient uv still wins for everything else on PATH,
        # since this only prepends one directory.
        env["PATH"] = os.pathsep.join(
            [str(Path(sys.executable).parent), os.environ.get("PATH", "")]
        )

    return DefinitionCommand(argv=argv, cwd=str((cwd or Path.cwd()).resolve()), env=env)


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
            f"{install_hint(package, extra)}"
        )
