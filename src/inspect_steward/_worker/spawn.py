"""Turn a scheduling decision into a running worker.

A worker is one process running one task (execution.md *Worker model*): it re-executes the definition so that every side effect of executing it is present, and a selection document intercepts `eval_set()` at its boundary so that only the selected task runs and no eval-set bookkeeping happens. That is what makes a flat, shared log directory safe — the only thing a worker touches is a file no other worker knows about.

Three things here are easy to get wrong and are therefore not left to the caller.

**Two log directories, deliberately different.** The selection's `log_dir` override reaches the `eval_set()` boundary and not one step earlier, so anything a frontend writes on its way there lands wherever it was *separately* told to. That work is once-per-run — resolved config, a requirements snapshot, a scan for prior logs — and every worker repeats it, so it is aimed at the worker's own scratch directory while the selection carries the run's. `spawn` builds the command itself rather than accepting one, so the two are always set together.

**The intent is recorded before the process exists.** A crash between `Popen` returning and a record of it landing leaves a worker nothing knows about, so `spawn` writes `intent` first and `launched` after — a safety property, and safety properties belong inside the mechanism they protect rather than in a bracket the caller is trusted to write. The cost of ordering it this way is an occasional record of a worker that never started, which `resolve_inflight` reports departed.

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
from inspect_ai._eval.eval_set_overrides import (
    EvalSetOverrides,
    merge_eval_set_overrides,
)
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
from .._notify import INSPECT_NOTIFICATION
from .._schedule import SpawnWorker
from .inflight import STEWARD_TASK, STEWARD_WORKER, record_intent, record_launched

MAX_KEY_LENGTH = 80
"""Longest display key a worker's file stem keeps. A key carries a task name, a solver, a model, and every distinguishing argument, so an argument sweep can produce a long one; the identifier hash beside it is what keeps two truncations apart."""


@dataclass(frozen=True)
class SpawnedWorker:
    """A worker that has been spawned.

    A handle rather than a record: everything here except `process` outlives this process, but nothing here is durable until the in-flight record is written.
    """

    worker: str
    """File stem, and the key everything else uses to name this worker: the selection document, the output file, and every line of the in-flight record."""

    identifiers: tuple[str, ...]
    """Task identifiers this worker was spawned for. One at the default width."""

    key: str
    """Display key from the manifest, for the task this worker is named after."""

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
    """Directory holding each worker's selection document and output (`.steward/workers/`). Absolute, which `Workspace` guarantees: it is what separates this workspace's workers from another's in the process table, and a relative one would be resolved against whatever directory happened to be scanning."""

    inflight: Path
    """The in-flight record (`.steward/inflight.jsonl`). Required rather than optional, because a worker spawned without one is a worker nothing can account for — and an argument that can be forgotten eventually is."""

    cwd: Path | None = None
    """Working directory for workers (defaults to the current directory, matching how the definition would run by hand)."""

    args: dict[str, Any] | None = None
    """Arguments for the definition (flow spec function args only)."""

    overrides: EvalSetOverrides | None = None
    """Inspect's own eval-set arguments for this run, as the committed manifest records them.

    Carried into every worker's selection so that the fleet runs what the manifest was enumerated under. It comes from the manifest rather than from this turn's environment for the reason the manifest exists: an 02:00 tend inherits no shell, and a worker running two epochs where the manifest counted one would make every progress figure wrong without failing anything.
    """

    scanners: dict[str, dict[str, Any]] | None = None
    """Scanners Steward injects beside the definition's own, as scout `ScannerSpec` dicts keyed by merge name (`Manifest.scan.injected`).

    From the manifest rather than this turn's directives, for the reason `overrides` is: the merge was settled and verified at launch, so an edited `scanners:` key changes nothing until a re-launch verifies it — a worker recording under a set nobody verified would be writing rows the next launch then refuses.
    """

    def spawn(self, action: SpawnWorker) -> SpawnedWorker:
        """Spawn a detached worker to run a share of the eval set.

        Args:
            action: The tasks to run, as `reconcile` decided them.

        Returns:
            The spawned worker.

        Raises:
            RuntimeError: On Windows, which cannot detach (see the module docstring).
            OSError: If the process cannot be started. A tend with other spawns to make decides what that means; there is nothing to clean up, because the selection document is inert and the `intent` already written is precisely how the next resolve learns this attempt is over.
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
        stem = self._name(action)

        # the frontend channel gets this worker's own scratch directory, never
        # the run's: what a definition writes on its way to `eval_set()` is
        # once-per-run work that every worker repeats, so aimed at the shared
        # directory it is N concurrent writes to the same paths
        scratch = self.workers_dir / stem
        command = definition_command(
            self.definition, self.type, self.args, self.cwd, log_dir=str(scratch)
        )

        # canonical, because this one leaves the process: it goes into the
        # worker's environment, where the only reader is a scan resolving it
        # from some other directory entirely
        selection = (self.workers_dir / f"{stem}.json").resolve()
        # utf-8 explicitly: inspect reads this file as bytes and parses it as
        # utf-8, so a locale-encoded write would make a non-ASCII identifier or
        # path unreadable on the other side of a boundary it cannot negotiate
        selection.write_text(
            worker_selection(
                action,
                eval_set_id=self.eval_set_id,
                log_dir=self.log_dir,
                overrides=self.overrides,
                scanners=self.scanners,
            ).model_dump_json(exclude_none=True, indent=2),
            encoding="utf-8",
        )

        # appended rather than truncated, belt to `_name`'s braces: nothing
        # should reach an existing stem now, and if something does, interleaving
        # with a worker that may still be running beats erasing its output
        output = self.workers_dir / f"{stem}.log"

        record_intent(
            self.inflight,
            worker=stem,
            tasks=action.tasks,
            selection=selection,
            argv=command.argv,
            cwd=command.cwd,
            log_dir=self.log_dir,
        )
        with output.open("ab") as stream:
            process = subprocess.Popen(
                command.argv,
                cwd=command.cwd,
                env=_worker_env(
                    command,
                    selection,
                    worker=stem,
                    identifiers=action.identifiers,
                ),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        record_launched(self.inflight, worker=stem, pid=process.pid)

        return SpawnedWorker(
            worker=stem,
            identifiers=action.identifiers,
            key=action.first.key,
            pid=process.pid,
            selection=selection,
            output=output,
            scratch=scratch,
            command=command,
            process=process,
        )

    def _name(self, action: SpawnWorker) -> str:
        """This worker's stem, with its attempt number free to advance.

        `SpawnTask.attempt` counts what the *decision layer* could see — the logs in the directory and the in-flight record — and both of those can be lost, since `.steward/` is a directory the design tells people they may delete. Two landed logs and a discarded record would number the next attempt 3 when 3 has already been used, and the stem is not merely a label: it names the selection document a live worker is reading and the entry the record folds on, so a collision loses one of the two attempts entirely.

        The directory it is about to write into is the one witness that outlives the record, so the number is advanced past whatever is already there. Ordinarily nothing is, and this costs one `exists()`.
        """
        attempt = max(task.attempt for task in action.tasks)
        while (self.workers_dir / f"{worker_stem(action, attempt)}.json").exists():
            attempt += 1
        return worker_stem(action, attempt)


def worker_selection(
    action: SpawnWorker,
    *,
    eval_set_id: str,
    log_dir: str,
    overrides: EvalSetOverrides | None = None,
    scanners: dict[str, dict[str, Any]] | None = None,
) -> EvalSetSelection:
    """Build the selection document for one worker.

    There is no upstream writer — the models are a wire format external runners build — so the version is declared from the installed inspect. That cannot skew: a worker is this interpreter.

    **The run's overrides and this worker's are one container, merged here.** Inspect would accept a run-wide document by environment variable as readily, and Steward does not use it: a worker's overrides then live in two places, one of them a file under `.steward/` that this design tells people they may delete. Merging into the selection leaves exactly one document per worker, written from the manifest the fleet is converging on.

    Args:
        action: The tasks to run.
        eval_set_id: Eval set id to stamp into the worker's logs.
        log_dir: Log directory for the worker.
        overrides: Inspect's arguments for the run, from the committed manifest. This worker's three own values are applied over them.
        scanners: Scanners the worker realizes and merges with the definition's own — a selection field of its own rather than an override, because injection merges where an override replaces.

    Returns:
        The selection.
    """
    return EvalSetSelection(
        version=EVAL_SET_SELECTION_VERSION,
        eval_set_id=eval_set_id,
        scanners=scanners or None,
        tasks=[
            EvalSetSelectionTask(
                identifier=task.identifier,
                resume=task.resume,
                # the pruning facets, which cost a worker nothing to receive
                # and save it constructing every task in the eval set to find
                # its own. Written for every task or for none: inspect prunes
                # only against a complete set, since a selection describing
                # some of its tasks would prune exactly the ones it left out
                registry_name=task.registry_name,
                args_hash=task.args_hash,
            )
            for task in action.tasks
        ],
        # always present rather than conditional: `log_dir` is what puts a
        # worker's logs where Steward is watching, so there is no Steward worker
        # that wants the definition's directory
        overrides=merge_eval_set_overrides(
            overrides,
            EvalSetOverrides(
                log_dir=log_dir,
                max_samples=action.max_samples,
                # likewise unconditional, and for a sharper reason: every other
                # override left unset falls back to what the definition passed,
                # but `eval_set()` fills `max_tasks` in below the selection
                # branch, so an unset one falls through to `eval()`'s rule
                # instead -- one task at a time for a single model, the model
                # count for several. A packed worker would run its batch
                # sequentially with nobody having chosen that. The whole batch,
                # because how much runs at once is bounded fleet-wide by the
                # pour, not here
                max_tasks=len(action.tasks),
                # **the half of the channel that exporting a variable does not
                # buy.** `build_apprise(True)` reads
                # `INSPECT_EVAL_NOTIFICATION`, but a worker's `eval_set()` only
                # calls it when its own `notification` argument is truthy — so
                # a fleet handed the value and not this one is a fleet that
                # never notifies, silently, while Steward posts normally.
                # Conditional on the variable rather than on a setting, because
                # that is what `_notify.channel` has already settled: present
                # for a channel from either vocabulary, absent when there is
                # none, and absent leaves whatever the definition chose
                notification=True
                if os.environ.get(INSPECT_NOTIFICATION, "").strip()
                else None,
            ),
        ),
    )


def worker_stem(action: SpawnWorker, attempt: int | None = None) -> str:
    """File stem for a worker's selection document and output.

    Readable first (the display key), unique second: the identifier hash is what keeps two tasks whose keys sanitize to the same string — or truncate to it — from writing over each other, and the attempt keeps a retry's evidence beside the attempt it replaced.

    A packed worker is named after its first task and says how many more it holds. The digest covers **every** identifier it was given, sorted, so two batches sharing a first task are still distinguishable — and because the digest of a one-element join is the digest of that element, a worker at the default width gets exactly the stem it did before packing existed.

    Args:
        action: The tasks to run.
        attempt: Attempt number to use in place of the action's, for a caller that has had to advance past a stem already taken (`Fleet._name`).

    Returns:
        The stem, with no extension.
    """
    joined = "\n".join(sorted(action.identifiers))
    digest = hashlib.sha256(joined.encode()).hexdigest()[:8]
    name = action.first.key
    if len(action.tasks) > 1:
        # a hyphen rather than a `+`, which `safe_filename` rewrites to the
        # underscore the stem already uses as its field separator
        name = f"{name}-plus{len(action.tasks) - 1}"
    key = safe_filename(name, max_length=MAX_KEY_LENGTH)
    number = (
        attempt if attempt is not None else max(task.attempt for task in action.tasks)
    )
    return f"{key}_{digest}_{number}"


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


def _worker_env(
    command: DefinitionCommand,
    selection: Path,
    *,
    worker: str,
    identifiers: tuple[str, ...],
) -> dict[str, str]:
    """Environment for a worker: the ambient one, plus worker mode, plus who it is.

    The selection path is inspect's marker and is what scopes a process to this workspace. `STEWARD_WORKER` and `STEWARD_TASK` are Steward's own, and they are what a scan reads instead of opening the selection document — so a worker stays identifiable after `.steward/` is deleted out from under it (`inflight.py`).

    `STEWARD_TASK` holds one identifier per line rather than gaining a plural sibling. A worker at the default width therefore exports exactly the value it always did, so the scan needs no branch and no version of it can be confused about which variable to read.

    `INSPECT_EVAL_SET_CAPTURE` is removed rather than left alone. Capture and selection are mutually exclusive, so an exported capture path — from a shell where someone was reading a definition by hand — would turn every worker into a startup error.
    """
    env = {
        **os.environ,
        **command.env,
        INSPECT_EVAL_SET_SELECTION: str(selection),
        STEWARD_WORKER: worker,
        STEWARD_TASK: "\n".join(identifiers),
        # no terminal to draw on, and the output file is read by people and by
        # grep rather than by a renderer
        "INSPECT_DISPLAY": "plain",
    }
    env.pop(INSPECT_EVAL_SET_CAPTURE, None)
    return env
