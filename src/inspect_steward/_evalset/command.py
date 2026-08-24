import importlib.util
import os
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from .detect import DefinitionType, install_hint


@dataclass(frozen=True)
class DefinitionCommand:
    """A command that executes an eval set definition.

    The command is mode-agnostic: run as-is it executes the definition normally; with `INSPECT_EVAL_SET_CAPTURE` set it enumerates; with `INSPECT_EVAL_SET_SELECTION` set it runs one worker's tasks.
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
        log_dir: Scratch directory for the definition's pre-boundary work (flow definitions only; other definition types carry their own log directory). Always a scratch directory, never the run's: a frontend writes artifacts and scans for prior logs *before* `eval_set()` is reached, where a selection's `log_dir` override cannot yet apply, and that work is once-per-run rather than once-per-worker. The run's log directory reaches the eval through the selection.

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
            # flow.yaml, the requirements freeze, and the pre-run log scan all
            # key off this directory, and all three are once-per-run work that
            # every worker repeats. Aimed at scratch they cost a little time;
            # aimed at the run's directory they are concurrent writes to shared
            # paths and a scan whose cost grows with the run. --no-log-dir-
            # create-unique keeps the path the one we named.
            argv += ["--log-dir", log_dir, "--no-log-dir-create-unique"]
        # the store's two halves are Steward's, not a worker's: the read half
        # runs before the boundary against the whole spec rather than this
        # worker's task, and the write half is only available to flow
        # definitions (execution.md *Steward owns both halves*).
        argv += ["--no-store-read", "--no-store-write"]
        # in this interpreter, as hawk's --direct below. Left alone, a spec
        # declaring `execution_type: venv` builds a venv per worker and runs
        # the eval in a grandchild -- so the pid Steward recorded, and every
        # liveness check keyed on it, would name the wrong process. That venv
        # is also where flow applies `dependencies` and `python_version`, which
        # the override therefore drops: see `warn_if_venv_declared`.
        argv += ["--set", "execution_type=inproc"]
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


def warn_if_venv_declared(path: Path, type: DefinitionType) -> None:
    """Warn when a flow spec asks for a virtualenv Steward will not build.

    `definition_command` overrides `execution_type` to `inproc` for every flow definition, and flow applies `dependencies` and `python_version` only when it builds the venv — so a spec that asks for either loses it without a word from flow. Saying so once, when the definition is read, is cheaper than an author finding it as an ImportError in one worker's output file.

    The check is deliberately shallow: a top-level declaration in a YAML spec, which is where an author writes one. A spec that inherits `execution_type` through `include:`, or a Python definition that builds its `FlowSpec` in code, is not seen. Resolving those means running flow's own loader against the spec, and the coupling costs more than the cases it would add.

    Args:
        path: Path to the definition file.
        type: Definition type.
    """
    if type != "flow" or path.suffix.lower() not in (".yaml", ".yml"):
        return
    try:
        loaded = yaml.safe_load(path.read_bytes())
    except yaml.YAMLError:
        # not this function's to diagnose: whatever reads the definition next
        # fails on it, with a better message than a warning could give
        return
    if not isinstance(loaded, dict):
        return
    if cast(dict[str, Any], loaded).get("execution_type") != "venv":
        return

    warnings.warn(
        f"'{path.name}' declares execution_type: venv, which Steward runs as "
        "inproc — one process per task is Steward's isolation model, and a "
        "virtualenv per worker is a second one, running the eval in a "
        "grandchild process nothing is tracking. The spec's dependencies and "
        "python_version are therefore not applied: the environment Steward is "
        "running in is the environment the eval runs in.",
        stacklevel=2,
    )


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
