import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from inspect_ai._eval.eval_set_manifest import EvalSetCapture

from .command import DefinitionCommand, definition_command, warn_if_venv_declared
from .cost import CaptureCost, measure
from .detect import DefinitionType, detect_definition_type
from .display import compute_display_keys
from .manifest import (
    MANIFEST_VERSION,
    Manifest,
    ManifestSource,
    ManifestTask,
    definition_hash,
)

INSPECT_EVAL_SET_CAPTURE = "INSPECT_EVAL_SET_CAPTURE"

_STDERR_TAIL_BYTES = 8192


class ReadEvalSetError(Exception):
    """An eval set definition could not be read."""

    def __init__(
        self,
        message: str,
        command: list[str],
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        stderr = stderr[-_STDERR_TAIL_BYTES:]
        detail = f"\ncommand: {' '.join(command)}"
        if returncode is not None:
            detail += f"\nexit code: {returncode}"
        if stderr.strip():
            detail += f"\nstderr:\n{stderr}"
        super().__init__(f"{message}{detail}")
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


def read_eval_set(
    definition: str | Path,
    args: dict[str, Any] | None = None,
    *,
    type: DefinitionType | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> Manifest:
    """Read the static definition of an eval set.

    Executes the definition in a subprocess with eval-set capture enabled: the definition runs normally (including any side effects) up to its `eval_set()` call, which resolves all tasks, writes a manifest, and exits without running anything.

    Args:
        definition: Path to the definition file (an `eval_set()` script, an Inspect Flow spec, or a Hawk eval set config).
        args: Arguments for the definition (flow spec function args only).
        type: Explicit definition type (auto-detected by default).
        cwd: Working directory for executing the definition (defaults to the current working directory, matching how the definition would run by hand).
        env: Additional environment variables for the definition process.
        timeout: Timeout in seconds for executing the definition.

    Returns:
        Manifest enumerating the resolved tasks of the eval set.

    Raises:
        ValueError: If the definition type cannot be determined or is invalid.
        ReadEvalSetError: If executing the definition fails or it produces no manifest.
    """
    definition_path = Path(definition)
    if not definition_path.exists():
        raise ValueError(f"Definition file '{definition_path}' does not exist.")

    resolved_type = detect_definition_type(definition_path, type)
    # here rather than in `definition_command`, which every worker calls: the
    # definition is read once per launch, so this is where a once-per-run
    # observation about the definition belongs
    warn_if_venv_declared(definition_path, resolved_type)

    with tempfile.TemporaryDirectory() as tmp_dir:
        # a scratch log directory keeps pre-boundary side effects (e.g. the
        # flow.yaml flow writes before its eval_set() call) out of the
        # definition's real log directory. Workers do the same with a directory
        # of their own -- the frontend channel never carries the run's log
        # directory, for either a read or a run.
        command = definition_command(
            definition_path,
            resolved_type,
            args=args,
            cwd=Path(cwd) if cwd is not None else None,
            log_dir=str(Path(tmp_dir) / "logs"),
        )
        measured: list[CaptureCost] = []
        capture = _run_capture(
            command,
            Path(tmp_dir) / "manifest.json",
            env=env,
            timeout=timeout,
            cost=measured,
        )

    keys = compute_display_keys(capture.tasks)
    return Manifest(
        version=MANIFEST_VERSION,
        identifier_version=capture.identifier_version,
        eval_set_id=capture.eval_set_id,
        source=ManifestSource(
            type=resolved_type,
            path=str(definition),
            content_hash=definition_hash(definition_path),
            capture_rss=measured[0].peak_rss if measured else None,
            args=args or {},
        ),
        options=capture.options,
        tasks=[
            ManifestTask(**task.model_dump(), key=key)
            for task, key in zip(capture.tasks, keys, strict=True)
        ],
    )


def _run_capture(
    command: DefinitionCommand,
    manifest_path: Path,
    env: dict[str, str] | None,
    timeout: float | None,
    cost: list[CaptureCost],
) -> EvalSetCapture:
    process_env = {
        **os.environ,
        **command.env,
        **(env or {}),
        INSPECT_EVAL_SET_CAPTURE: str(manifest_path),
        "INSPECT_DISPLAY": "plain",
    }
    # `Popen` rather than `subprocess.run`, for the one thing `run` cannot give:
    # a pid to watch while the definition executes. What is being watched is the
    # ceiling on what a worker's startup costs (`cost.py`)
    try:
        with subprocess.Popen(
            command.argv,
            cwd=command.cwd,
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            sampler = measure(process.pid)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # the definition is still running and holding the pipes; kill it
                # before draining them, which is what `subprocess.run` does too
                process.kill()
                process.communicate()
                raise
            finally:
                cost.append(CaptureCost(peak_rss=sampler.stop()))
        result = subprocess.CompletedProcess(
            command.argv, process.returncode, stdout, stderr
        )
    except subprocess.TimeoutExpired as ex:
        stderr = ex.stderr
        raise ReadEvalSetError(
            f"Timed out reading the eval set definition (after {timeout} seconds).",
            command=command.argv,
            stderr=stderr.decode() if isinstance(stderr, bytes) else stderr or "",
        ) from ex

    if result.returncode != 0:
        raise ReadEvalSetError(
            "The eval set definition failed before reaching eval_set().",
            command=command.argv,
            returncode=result.returncode,
            stderr=result.stderr,
        )
    if not manifest_path.exists():
        raise ReadEvalSetError(
            "The eval set definition never called eval_set() "
            "(is this the right file or definition type?).",
            command=command.argv,
            returncode=result.returncode,
            stderr=result.stderr,
        )
    try:
        return EvalSetCapture.model_validate_json(manifest_path.read_bytes())
    except ValueError as ex:
        raise ReadEvalSetError(
            "The captured eval set manifest is not valid (this can indicate "
            "an inspect-ai/inspect-steward version mismatch — try upgrading "
            f"both):\n{ex}",
            command=command.argv,
            returncode=result.returncode,
            stderr=result.stderr,
        ) from ex
