"""Turn a scheduling decision into a running worker.

A worker is one process running one task (execution.md *Worker model*): it re-executes the definition so that every side effect of executing it is present, and a selection document intercepts `eval_set()` at its boundary so that only the selected task runs and no eval-set bookkeeping happens. That is what makes a flat, shared log directory safe — the only thing a worker touches is a file no other worker knows about.

Two things here are easy to get wrong and are therefore not left to the caller.

**Two log directories, deliberately different.** The selection's `log_dir` override reaches the `eval_set()` boundary and not one step earlier, so anything a frontend writes on its way there lands wherever it was *separately* told to. That work is once-per-run — resolved config, a requirements snapshot, a scan for prior logs — and every worker repeats it, so it is aimed at the worker's own scratch directory while the selection carries the run's. `spawn` builds the command itself rather than accepting one, so the two are always set together.

**Workers are detached.** `start_new_session` puts each worker in its own session, so a Ctrl-C or a hangup aimed at the tend that spawned it does not reach it and the run survives the process that started it. Detached is not the same as reparented: a worker stays this process's child until this process exits, which is why `SpawnedWorker` can carry a live handle at all.

This is the mechanism that makes Steward POSIX-only. Windows ignores `start_new_session`, so a worker there would stay attached to its console and die with it — the guarantee failing silently rather than loudly. Windows is declined rather than deferred (execution.md *Detachment and the in-flight record*).
"""

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspect_ai._eval.eval_set_manifest import INSPECT_EVAL_SET_CAPTURE
from inspect_ai._eval.eval_set_selection import (
    EVAL_SET_SELECTION_VERSION,
    INSPECT_EVAL_SET_SELECTION,
    EvalSetSelection,
    EvalSetSelectionTask,
)
from inspect_ai._eval.evalset import eval_set_id_for_log_dir
from inspect_ai._util.file import safe_filename

from .._evalset.command import DefinitionCommand, definition_command
from .._evalset.detect import DefinitionType
from .._schedule import SpawnWorker

MAX_KEY_LENGTH = 80
"""Longest display key a worker's file stem keeps. A key carries a task name, a solver, a model, and every distinguishing argument, so an argument sweep can produce a long one; the identifier hash beside it is what keeps two truncations apart."""


@dataclass(frozen=True)
class SpawnedWorker:
    """A worker that has been spawned.

    A handle rather than a record: everything here except `process` outlives this process, but nothing here is durable until the in-flight record is written.
    """

    identifier: str
    """Task identifier this worker was spawned for."""

    key: str
    """Display key from the manifest."""

    pid: int
    """Process id."""

    selection: Path
    """The selection document written for this worker. Also how the process table identifies it: the path is in the worker's environment, which is the only marker every definition type can carry (a frontend's CLI would reject an extra argument)."""

    output: Path
    """Merged stdout and stderr. The only evidence a worker leaves during the window before its eval starts, where neither the log directory nor control discovery can see it."""

    scratch: Path
    """Where the definition's pre-boundary work lands (a frontend's resolved config, requirements snapshot, and prior-log scan). One per worker, so N of them never write the same path; empty for a definition that does no such work."""

    command: DefinitionCommand
    """The command the worker is running."""

    process: subprocess.Popen[bytes] = field(repr=False, compare=False)
    """Live handle, valid only while this process lives. A detached worker is still this process's child until it exits, so it can be waited on here — but a tend never outlives its workers, so the record is what actually tracks them."""


@dataclass(frozen=True)
class Fleet:
    """What every worker for one definition shares.

    Built once per run and spawned from many times, because building the command resolves the definition path and checks the packages a definition type needs, and because the invariants below are exactly the ones that must not vary between two workers writing into one directory.
    """

    definition: Path
    """Path to the definition file."""

    type: DefinitionType
    """Definition type."""

    log_dir: str
    """Log directory every worker writes into."""

    eval_set_id: str
    """Eval set id stamped into every log (see `resolve_eval_set_id`)."""

    workers_dir: Path
    """Directory holding each worker's selection document and output (`.steward/workers/`)."""

    cwd: Path | None = None
    """Working directory for workers (defaults to the current directory, matching how the definition would run by hand)."""

    args: dict[str, Any] | None = None
    """Arguments for the definition (flow spec function args only)."""

    def spawn(self, action: SpawnWorker) -> SpawnedWorker:
        """Spawn a detached worker to run one task.

        Args:
            action: The task to run, as `reconcile` decided it.

        Returns:
            The spawned worker.

        Raises:
            RuntimeError: On Windows, which cannot detach (see the module docstring).
            OSError: If the process cannot be started. A tend with other spawns to make decides what that means; there is nothing to clean up, since the selection document is inert.
        """
        # before anything is written, so a refusal leaves nothing behind. The
        # package declares itself POSIX-only, but a classifier is metadata and
        # this is the one operation that would otherwise misbehave *silently*
        if sys.platform == "win32":
            raise RuntimeError(
                "Steward requires macOS or Linux. Windows ignores "
                "`start_new_session`, so a worker would stay attached to the "
                "console that spawned it and die with it — and a run that "
                "outlives its supervisor is the guarantee Steward exists to "
                "make."
            )

        self.workers_dir.mkdir(parents=True, exist_ok=True)
        stem = worker_stem(action)

        # the frontend channel gets this worker's own scratch directory, never
        # the run's: what a definition writes on its way to `eval_set()` is
        # once-per-run work that every worker repeats, so aimed at the shared
        # directory it is N concurrent writes to the same paths
        scratch = self.workers_dir / stem
        command = definition_command(
            self.definition, self.type, self.args, self.cwd, log_dir=str(scratch)
        )

        selection = self.workers_dir / f"{stem}.json"
        # utf-8 explicitly: inspect reads this file as bytes and parses it as
        # utf-8, so a locale-encoded write would make a non-ASCII identifier or
        # path unreadable on the other side of a boundary it cannot negotiate
        selection.write_text(
            worker_selection(
                action, eval_set_id=self.eval_set_id, log_dir=self.log_dir
            ).model_dump_json(exclude_none=True, indent=2),
            encoding="utf-8",
        )

        # appended rather than truncated: a respawn of the same attempt (which
        # only happens when the in-flight record was lost) then interleaves with
        # a worker that may still be running rather than erasing its output
        output = self.workers_dir / f"{stem}.log"
        with output.open("ab") as stream:
            process = subprocess.Popen(
                command.argv,
                cwd=command.cwd,
                env=_worker_env(command, selection),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        return SpawnedWorker(
            identifier=action.identifier,
            key=action.key,
            pid=process.pid,
            selection=selection,
            output=output,
            scratch=scratch,
            command=command,
            process=process,
        )


def worker_selection(
    action: SpawnWorker, *, eval_set_id: str, log_dir: str
) -> EvalSetSelection:
    """Build the selection document for one worker.

    There is no upstream writer — the models are a wire format external runners build — so the version is declared from the installed inspect. That cannot skew: a worker is this interpreter.

    Args:
        action: The task to run.
        eval_set_id: Eval set id to stamp into the worker's logs.
        log_dir: Log directory for the worker.

    Returns:
        The selection.
    """
    return EvalSetSelection(
        version=EVAL_SET_SELECTION_VERSION,
        eval_set_id=eval_set_id,
        tasks=[
            EvalSetSelectionTask(identifier=action.identifier, resume=action.resume)
        ],
        log_dir=log_dir,
        max_samples=action.max_samples,
    )


def worker_stem(action: SpawnWorker) -> str:
    """File stem for a worker's selection document and output.

    Readable first (the display key), unique second: the identifier hash is what keeps two tasks whose keys sanitize to the same string — or truncate to it — from writing over each other, and the attempt keeps a retry's evidence beside the attempt it replaced.

    Args:
        action: The task to run.

    Returns:
        The stem, with no extension.
    """
    digest = hashlib.sha256(action.identifier.encode()).hexdigest()[:8]
    key = safe_filename(action.key, max_length=MAX_KEY_LENGTH)
    return f"{key}_{digest}_{action.attempt}"


def resolve_eval_set_id(log_dir: str, eval_set_id: str | None = None) -> str:
    """Read, or mint and write, the log directory's eval set id.

    Worker mode never touches `.eval-set-id` (execution.md *What worker mode deliberately skips*), so this is Steward's to do, once, at run start: every worker is told the id to stamp, and a directory that already has one keeps it.

    Args:
        log_dir: Log directory.
        eval_set_id: Id to use, if the definition named one. `None` mints one.

    Returns:
        The eval set id.

    Raises:
        PrerequisiteError: If `eval_set_id` conflicts with the one the directory already has.
    """
    return eval_set_id_for_log_dir(log_dir, eval_set_id)


def _worker_env(command: DefinitionCommand, selection: Path) -> dict[str, str]:
    """Environment for a worker: the ambient one, plus worker mode.

    `INSPECT_EVAL_SET_CAPTURE` is removed rather than left alone. Capture and selection are mutually exclusive, so an exported capture path — from a shell where someone was reading a definition by hand — would turn every worker into a startup error.
    """
    env = {
        **os.environ,
        **command.env,
        INSPECT_EVAL_SET_SELECTION: str(selection),
        # no terminal to draw on, and the output file is read by people and by
        # grep rather than by a renderer
        "INSPECT_DISPLAY": "plain",
    }
    env.pop(INSPECT_EVAL_SET_CAPTURE, None)
    return env
