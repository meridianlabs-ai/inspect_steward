"""Run eval-set workers by hand, for the case production never has.

Steward spawns one task per worker, detached (`inspect_steward._worker`), and
`tests/worker/test_spawn.py` exercises that. The protocol allows a selection to
carry several tasks, and this runs one that does — which is what keeps a
fifteen-task identity fixture down to a single interpreter startup instead of
fifteen. Attached and blocking, so a caller gets a return code and output back.

Not named `test_*`, so pytest does not collect it.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from inspect_ai._eval.eval_set_selection import (
    EVAL_SET_SELECTION_VERSION,
    INSPECT_EVAL_SET_SELECTION,
    EvalSetSelection,
    EvalSetSelectionTask,
)
from inspect_ai.log import EvalLog, list_eval_logs, read_eval_log
from inspect_steward._evalset.command import definition_command
from inspect_steward._evalset.detect import detect_definition_type

DEFAULT_EVAL_SET_ID = "steward-test-eval-set"


@dataclass(frozen=True)
class WorkerResult:
    """Outcome of running one worker."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def selection(
    identifiers: list[str],
    log_dir: Path,
    *,
    eval_set_id: str = DEFAULT_EVAL_SET_ID,
    resume: dict[str, str] | None = None,
    max_samples: int | None = None,
) -> EvalSetSelection:
    """Build a selection naming `identifiers`.

    Args:
        identifiers: Task identifiers from a capture manifest.
        log_dir: Log directory for the worker (the selection override, which works for every definition type where `definition_command`'s `log_dir` is flow-only).
        eval_set_id: Eval set id to stamp into the worker's logs.
        resume: Prior log location per identifier, for the tasks that resume one.
        max_samples: Sample concurrency override.

    Returns:
        The selection.
    """
    return EvalSetSelection(
        version=EVAL_SET_SELECTION_VERSION,
        eval_set_id=eval_set_id,
        tasks=[
            EvalSetSelectionTask(
                identifier=identifier, resume=(resume or {}).get(identifier)
            )
            for identifier in identifiers
        ],
        overrides=EvalSetOverrides(log_dir=str(log_dir), max_samples=max_samples),
    )


def run_workers(
    definition: Path,
    selections: list[EvalSetSelection],
    *,
    cwd: Path,
    timeout: float = 300,
) -> list[WorkerResult]:
    """Run one worker per selection, all of them concurrently.

    Every worker is launched before any is waited on, so a multi-worker call exercises the shared flat log directory rather than a sequence of solo runs.

    Args:
        definition: Path to the definition file.
        selections: One selection per worker.
        cwd: Working directory for the workers.
        timeout: Timeout in seconds for the whole fleet.

    Returns:
        One result per selection, in order.
    """
    # the selection's log_dir override arrives at the eval_set() boundary, which
    # is too late for anything a frontend writes on the way there: a flow worker
    # drops flow.yaml and flow-requirements.txt into the *definition's* log
    # directory first. So a worker needs both channels, exactly as a read does.
    log_dirs = {
        entry.overrides.log_dir for entry in selections if entry.overrides is not None
    }
    assert len(log_dirs) == 1, "workers in one call share a log directory"
    command = definition_command(
        definition,
        detect_definition_type(definition, None),
        log_dir=log_dirs.pop(),
    )

    processes: list[subprocess.Popen[str]] = []
    for index, entry in enumerate(selections):
        path = cwd / f"selection-{index}.json"
        path.write_text(json.dumps(entry.model_dump(exclude_none=True)))
        processes.append(
            subprocess.Popen(
                command.argv,
                cwd=str(cwd),
                env={
                    **os.environ,
                    **command.env,
                    INSPECT_EVAL_SET_SELECTION: str(path),
                    "INSPECT_DISPLAY": "plain",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    results: list[WorkerResult] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=timeout)
        results.append(
            WorkerResult(returncode=process.returncode, stdout=stdout, stderr=stderr)
        )
    return results


def landed_logs(log_dir: Path) -> list[EvalLog]:
    """Read the headers of every log in a directory.

    A header carries `plan` and the whole `eval` spec, which is everything `task_identifier` reads from a log — and reading headers rather than samples is what makes correlation affordable over a directory of thousands.

    Args:
        log_dir: Log directory to read.

    Returns:
        Log headers, in no particular order.
    """
    return [
        read_eval_log(info, header_only=True) for info in list_eval_logs(str(log_dir))
    ]
