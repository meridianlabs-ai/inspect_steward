"""The marking runner: one detached process that writes one ruling into its logs.

Spawned by the tend's executor with a run id (`spawn.py`), it reads everything else from the runs record and the journal, does its work, journals the application, and records how it ended. Its posture is a worker's: nothing tends it, it leaves a record of every step so that the next turn can retry exactly what did not land, and a failure costs this run and never the tend.

**The capture guard goes on before anything of the eval's is touched.** A definition that calls `eval_set()` at module level would run the eval if anything here imported it — in a process meant to edit one file. With `INSPECT_EVAL_SET_CAPTURE` set, an inline `eval_set()` enumerates into a scratch path instead, which is the launch's own guard (`_evalset.read`). The fleet strips the variable from the side workers it spawns, so they run.

**A zero is the scorer's verdict on an empty attempt, obtained by running it.** The side run spawns the definition into `.steward/marks/<run>/logs`, one log directory per task, selecting just the ruled sample ids and asking Inspect to run them with its `empty_attempt()` solver: `Task.setup` runs, the task's own solver does not, and the task's scorer scores the untouched state. Inspect's `zero_samples` then copies those verdicts into the main log (`edit.zero_targets`), history and provenance intact. Samples whose log already carries this ruling (a run that crashed before its journal line) are booked first, so the side run covers only the samples still to edit.

**The main log is read and written under the workspace claim**, both halves inside it. A tend rewrites the same file for a re-run's invalidation or an acceptance, and a log read before the claim and written after it would put back what the tend changed in between.
"""

import os
import time
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspect_ai._eval.eval_set_manifest import INSPECT_EVAL_SET_CAPTURE
from inspect_ai._eval.eval_set_overrides import (
    EvalSetOverrides,
    merge_eval_set_overrides,
)
from inspect_ai.log import EvalLog, list_eval_logs, read_eval_log

from .._anomaly.applied import RULING_APPLIED, read_applied
from .._anomaly.fold import read_anomalies
from .._anomaly.model import Anomalies, Anomaly, Disposition, Ruling
from .._evalset.classify import digest8
from .._evalset.manifest import (
    ManifestTask,
    read_manifest,
    worker_overrides,
)
from .._scan import ScanError, initialize_scan
from .._schedule import SpawnTask, SpawnWorker
from .._smoke.run import reap
from .._worker import (
    Fleet,
    resolve_eval_set_id,
    resolve_inflight,
    tail,
)
from .._workspace import (
    ACTION,
    Claim,
    Held,
    Workspace,
    acquire,
    append_event,
    read_journal,
    steward_log,
)
from .edit import (
    Marked,
    Target,
    commit,
    exclude_targets,
    locate,
    marked_by,
    zero_targets,
)
from .state import read_runs, record_exited

MARK_POLL = 0.5
"""Seconds between looks at whether a side run's workers have exited."""

MARK_CAP = 30
"""Minutes a side run may take before it is reaped and the run fails. An empty attempt runs only the task's setup and scorer; what this bounds is a worker that hangs or never lands a log."""

CLAIM_WAIT = 120.0
"""Seconds to wait for the workspace claim. A tend holds it for seconds; a launch or a smoke for longer, and those are worth failing this run over rather than editing beside."""

CLAIM_POLL = 2.0

TAIL_LINES = 40
"""Lines of each side worker's output a failed side run carries in its error. The worker's own last words are the only copy of why it wrote nothing; this many lines holds a traceback and the line before it."""


def run_mark(workspace: Workspace, run: str) -> int:
    """Carry out one run: the hidden command's body.

    Args:
        workspace: The workspace, found from the runner's working directory.
        run: The run id the executor recorded.

    Returns:
        The exit status: `0` where the run did its work or found none left, else `1` with the failure in the record and the run's log.
    """
    directory = workspace.marks_run(run)
    directory.mkdir(parents=True, exist_ok=True)
    # before anything else: the first import of the eval's module is the
    # moment an inline `eval_set()` would run
    os.environ[INSPECT_EVAL_SET_CAPTURE] = str(directory / "capture.json")
    try:
        detail = _carry_out(workspace, run, directory)
    except Exception as ex:
        message = f"{type(ex).__name__}: {ex}"
        print(f"marking run {run} failed: {message}", flush=True)
        steward_log(workspace.log, f"marking run {run} failed: {message}")
        record_exited(workspace.marks_runs, run=run, status=1, detail=message)
        return 1
    print(f"marking run {run}: {detail}", flush=True)
    record_exited(workspace.marks_runs, run=run, status=0, detail=detail)
    return 0


def _carry_out(workspace: Workspace, run: str, directory: Path) -> str:
    """The run, start to finish. Raises on anything that stops it."""
    recorded = read_runs(workspace.marks_runs).get(run)
    if recorded is None:
        raise RuntimeError("no intent is recorded for this run")
    events = read_journal(workspace.journal).events
    anomaly, ruling = _decision(
        read_anomalies(events), recorded.class_key, recorded.ruling_ts
    )
    applied = read_applied(events)
    done = applied.edited_uuids(recorded.class_key, recorded.ruling_ts)
    targets = [target for target in recorded.targets if target.uuid not in done]
    if not targets:
        return "nothing left to write"

    edited: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    failure: Exception | None = None
    empty: dict[str, EvalLog] = {}
    side_run: dict[str, Any] | None = None
    if recorded.disposition is Disposition.ZERO:
        # a zero a crashed run wrote is booked before the side run, which only
        # the samples still to edit need: it may refuse them (an integer id)
        # or fail, and either would leave a written zero reported unwritten
        edited, targets = _found_written(targets, ruling)
        if targets:
            try:
                empty, side_run = _side_run(workspace, directory, targets)
            except Exception as ex:
                failure = ex

    by_location: dict[str, list[Target]] = {}
    if failure is None:
        for target in targets:
            by_location.setdefault(target.location, []).append(target)
    # nothing left to write takes no claim
    with _claim(workspace) if by_location else nullcontext():
        for location, group in by_location.items():
            log = read_eval_log(location)
            if log.status == "started":
                deferred.extend(
                    _deferral(target, "the log is still being written")
                    for target in group
                )
                continue
            try:
                if recorded.disposition is Disposition.EXCLUDE:
                    marked = exclude_targets(log, group, anomaly, ruling)
                else:
                    # `_side_run` holds every target's task, or has raised
                    marked = zero_targets(
                        log, empty[group[0].task], group, anomaly, ruling
                    )
                if marked.edited:
                    commit(log, location)
            except Exception as ex:
                # Inspect refuses a log whole, before any edit. The logs
                # already written are journaled below before this run fails,
                # so the report does not call a written log unwritten
                failure = ex
                break
            if marked.done:
                edited.append(_entry(group[0], marked))
            deferred.extend(_deferral(target, why) for target, why in marked.deferred)

    if edited:
        fields: dict[str, Any] = {
            "action": RULING_APPLIED,
            "class": recorded.class_key,
            "for": ruling.ts,
            "by": ruling.by,
            "disposition": recorded.disposition.value,
            "run": run,
            "edited": edited,
        }
        if side_run is not None:
            fields["side_run"] = side_run
        if deferred:
            # provenance only, never memory: the remainder is recomputed next
            # turn as census-minus-applied
            fields["deferred"] = deferred
        append_event(workspace.journal, ACTION, **fields)
    if failure is not None:
        raise failure
    written = sum(len(entry["uuids"]) for entry in edited)
    summary = f"wrote {written} sample{'s' if written != 1 else ''}"
    if deferred:
        reasons = "; ".join(
            f"{entry['id']}:{entry['epoch']} in {entry['task']} ({entry['why']})"
            for entry in deferred
        )
        summary += f", deferred {len(deferred)}: {reasons}"
    return summary


def _found_written(
    targets: Sequence[Target], ruling: Ruling
) -> tuple[list[dict[str, Any]], list[Target]]:
    """Split off the targets whose log already carries this ruling.

    Read without the claim, since nothing is written here: a sample only gains a ruling's mark, so one found now is still found when the claim is held. A log still being written is left to the claimed pass, which defers it.

    Returns:
        The application entries for the targets found written, one per log, and the targets left.
    """
    entries: list[dict[str, Any]] = []
    left: list[Target] = []
    by_location: dict[str, list[Target]] = {}
    for target in targets:
        by_location.setdefault(target.location, []).append(target)
    for location, group in by_location.items():
        log = read_eval_log(location)
        found: list[Target] = []
        for target in group:
            sample = locate(log, target) if log.status != "started" else None
            if sample is not None and marked_by(sample, ruling.ts):
                found.append(target)
            else:
                left.append(target)
        if found:
            entries.append(_entry(found[0], Marked(found=found)))
    return entries, left


def _decision(
    anomalies: Anomalies, class_key: str, ruling_ts: str
) -> tuple[Anomaly, Ruling]:
    """The window and ruling this run carries out, as the journal now holds them."""
    windows = [
        anomaly
        for anomaly in (*anomalies.open, *anomalies.settled)
        if anomaly.class_key == class_key
        and anomaly.ruling is not None
        and anomaly.ruling.ts == ruling_ts
    ]
    if not windows:
        raise RuntimeError(
            f"the journal no longer holds a ruling on {class_key} at {ruling_ts}"
        )
    newest = max(windows, key=lambda anomaly: anomaly.generation)
    assert newest.ruling is not None
    return newest, newest.ruling


def _claim(workspace: Workspace) -> Claim:
    """The workspace claim, waited for rather than broken.

    A runner is spawned by a tend that holds the claim and ordinarily releases it seconds later; a launch or a smoke holds it for longer, and outwaiting either is not this process's business — it fails, and the executor's attempt budget decides what that means.
    """
    deadline = time.monotonic() + CLAIM_WAIT
    while True:
        outcome = acquire(workspace.claim, command="mark", break_stale=False)
        if not isinstance(outcome, Held):
            return outcome
        if time.monotonic() >= deadline:
            holder = outcome.command or "another command"
            raise RuntimeError(
                f"the workspace claim is held by {holder} (pid {outcome.pid}) and "
                f"did not free in {CLAIM_WAIT:.0f}s"
            )
        time.sleep(CLAIM_POLL)


def _entry(first: Target, marked: Marked) -> dict[str, Any]:
    """One log's line in the application event: what was written into it."""
    entry: dict[str, Any] = {
        "task": first.task,
        "location": first.location,
        "eval_id": first.eval_id,
        "uuids": [target.uuid for target in marked.done],
        "scores": sorted(marked.scores),
    }
    if marked.found:
        entry["found"] = len(marked.found)
    return entry


def _deferral(target: Target, why: str) -> dict[str, Any]:
    return {
        "task": target.task,
        "id": target.sample_id,
        "epoch": target.epoch,
        "why": why,
    }


# --- the side run --------------------------------------------------------


@dataclass
class _Side:
    """One task's share of the side run."""

    row: ManifestTask
    targets: list[Target]
    ids: list[int | str] = field(default_factory=list[int | str])
    """The typed sample ids the selection names — taken from the main log, since the census spells every id as a string."""

    selectors: list[str] = field(default_factory=list[str])
    """The same ids as the worker's `sample_id` override must spell them (see `_selectors`)."""

    log_dir: str = ""
    """This task's own log directory under the run's scratch. An empty-attempt log has an identifier of its own, so a directory per task is what pairs the log with its task."""

    worker: str | None = None
    output: Path | None = None
    """The worker's merged output, once spawned."""


def _side_run(
    workspace: Workspace, directory: Path, targets: Sequence[Target]
) -> tuple[dict[str, EvalLog], dict[str, Any]]:
    """Run an empty attempt at each target in a scratch run, and read its logs back.

    One worker per task, selecting exactly the ruled sample ids and running them with Inspect's `empty_attempt()` solver (the selection's `empty_attempt`). The scratch run is otherwise the definition's own — its setup, its limits, its scorer — under `worker_overrides`, exactly as the fleet runs it; only where it writes, which samples it runs and the solver differ.

    Returns:
        Each task's empty-attempt log, read whole and keyed by task identifier, and the record the application event carries about the run.
    """
    manifest = read_manifest(workspace.manifest)
    rows = {row.identifier: row for row in manifest.tasks}
    sides: dict[str, _Side] = {}
    for target in targets:
        row = rows.get(target.task)
        if row is None:
            raise RuntimeError(
                f"the manifest no longer names task {target.task}, so nothing "
                f"can run its scorer"
            )
        sides.setdefault(target.task, _Side(row=row, targets=[])).targets.append(target)
    log_dir = directory / "logs"
    for side in sides.values():
        side.ids = _typed_ids(side.targets)
        side.selectors = _selectors(side.row, side.ids)
        side.log_dir = str(log_dir / digest8(side.row.identifier))
    # Inspect zeroes only samples with string ids, so a side run for an
    # integer id would spend its setup and scorer on a verdict that cannot be
    # copied in
    integer = [
        f"{identifier} {id}"
        for identifier, side in sides.items()
        for id in side.ids
        if isinstance(id, int)
    ]
    if integer:
        raise RuntimeError(
            f"cannot zero samples with integer ids ({', '.join(integer)}): an "
            f"integer id need not name the same sample in another run. Exclude "
            f"them instead."
        )

    definition = Path(manifest.source.path)
    if not definition.is_absolute():
        definition = workspace.root / definition

    spawned: list[str] = []
    started = time.monotonic()
    try:
        for side in sides.values():
            eval_set_id = resolve_eval_set_id(side.log_dir)
            if manifest.scan is not None:
                # workers require the scan directory to exist and record into
                # it; a fresh id under the scratch directory keeps their rows
                # off the run's
                try:
                    initialize_scan(
                        manifest.scan, log_dir=side.log_dir, scan_id=eval_set_id
                    )
                except ScanError as ex:
                    steward_log(
                        workspace.log,
                        f"the side run could not bracket its scan: {ex}",
                    )
            fleet = Fleet(
                definition=definition,
                type=manifest.source.type,
                log_dir=side.log_dir,
                eval_set_id=eval_set_id,
                workers_dir=workspace.marks_workers,
                inflight=workspace.marks_inflight,
                cwd=workspace.root,
                args=manifest.source.args or None,
                # naming `sample_id` displaces `limit` and `sample_shuffle`,
                # which is upstream's rule and what a slice of named ids wants
                overrides=merge_eval_set_overrides(
                    worker_overrides(manifest),
                    EvalSetOverrides(sample_id=side.selectors),
                ),
                scanners=None,
                empty_attempt=True,
            )
            spawned_worker = fleet.spawn(
                SpawnWorker(
                    tasks=(
                        SpawnTask(
                            identifier=side.row.identifier,
                            key=side.row.key,
                            resume=None,
                            attempt=1,
                            reason=None,
                            registry_name=side.row.registry_name,
                            args_hash=side.row.args_hash,
                        ),
                    ),
                    max_samples=len(side.ids),
                )
            )
            side.worker = spawned_worker.worker
            side.output = spawned_worker.output
            spawned.append(side.worker)
        capped = _wait(workspace, spawned, started)
    finally:
        lingering = reap(
            workspace,
            spawned,
            inflight=workspace.marks_inflight,
            workers_dir=workspace.marks_workers,
        )
    if capped:
        raise RuntimeError(
            f"the side run did not land within {MARK_CAP} minutes and was stopped"
        )
    if lingering:
        raise RuntimeError(
            f"side worker(s) {', '.join(lingering)} were still running after "
            f"being cancelled"
        )

    empty, locations = _collect_empty_attempts(sides)
    return empty, {"log_dir": str(log_dir), "locations": locations}


def _collect_empty_attempts(
    sides: Mapping[str, _Side],
) -> tuple[dict[str, EvalLog], list[str]]:
    """Read each task's empty-attempt log back, or raise where a worker left none or an unsuccessful one.

    A side run that lands no log will land the same way next time: the selection or the definition is wrong, not the moment. Raising here is what makes the run fail rather than defer, so the attempts run out and the failure reaches the report — carrying the workers' own last words, since a worker that never got as far as a log said why only there. Whether the log holds a verdict for every target is `zero_samples`'s to check.

    Returns:
        Each task's empty-attempt log, keyed by task identifier, and their locations.
    """
    empty: dict[str, EvalLog] = {}
    locations: list[str] = []
    missing: list[str] = []
    unsuccessful: list[str] = []
    for identifier, side in sides.items():
        # not recursive: the scan directory lives beside the log
        landed = list_eval_logs(side.log_dir, recursive=False)
        if not landed:
            missing.append(identifier)
            continue
        newest = max(landed, key=lambda info: info.mtime or 0.0)
        log = read_eval_log(newest.name)
        if log.status != "success":
            # `zero_samples` would refuse it by its status alone; the cause is
            # in the log's error, or else in the worker's output
            cause = (
                log.error.message
                if log.error is not None
                else _worker_tails({identifier: side})
            )
            unsuccessful.append(
                f"the side run's log for {identifier} has status "
                f"'{log.status}' ({newest.name}): {cause}"
            )
            continue
        empty[identifier] = log
        locations.append(newest.name)
    if missing:
        raise RuntimeError(
            f"the side run landed no log for {', '.join(missing)} — its "
            f"workers exited without writing one. {_worker_tails(sides)}"
        )
    if unsuccessful:
        raise RuntimeError("\n\n".join(unsuccessful))
    return empty, locations


def _worker_tails(sides: Mapping[str, _Side]) -> str:
    """The end of each side worker's output, for an error about a run that wrote no log."""
    blocks: list[str] = []
    for side in sides.values():
        if side.worker is None or side.output is None:
            continue
        lines = tail(side.output).strip().splitlines()
        if lines:
            block = "\n".join(lines[-TAIL_LINES:])
            blocks.append(f"worker {side.worker} wrote:\n{block}")
    if blocks:
        return "\n\n".join(blocks)
    outputs = [str(side.output.parent) for side in sides.values() if side.output]
    where = ", ".join(dict.fromkeys(outputs)) or "the side workers' directory"
    return f"see the side worker logs in {where}"


def _selectors(row: ManifestTask, ids: Sequence[int | str]) -> list[str]:
    """The ids as the side worker's `sample_id` override must spell them.

    A workaround for upstream: `resolve_task_sample_ids` reads everything before the first `:` of an id as a task-scoping prefix and drops the id when that prefix is not the task's name — so a corpus id like `user:cybergym/arvo_6008` leaves an empty filter, and the worker exits having run nothing and written no log. Qualifying every id with the task's name round-trips such an id and is a no-op for an int or a colon-free one. The *effective* name (`Task.name`), which is what the runtime resolves against: the registry name for a registered task, but an explicit `Task(name=...)` overrides it and an ad-hoc task has none. Remove once upstream stops splitting on the id's own colon — see `BRIEF-inspect-ai-sample-id-colon.md`.
    """
    return [f"{row.name}:{id}" for id in ids]


def _typed_ids(targets: Sequence[Target]) -> list[int | str]:
    """The ids a selection names, typed as the main log records them."""
    wanted = {target.sample_id for target in targets}
    ids: list[int | str] = []
    for location in dict.fromkeys(target.location for target in targets):
        for sample in read_eval_log(location).samples or []:
            if str(sample.id) in wanted and sample.id not in ids:
                ids.append(sample.id)
    return ids


def _wait(workspace: Workspace, spawned: Sequence[str], started: float) -> bool:
    """Wait until no spawned side worker is left in the process table — a worker that has landed its log has exited — or the cap fires.

    Returns:
        Whether the cap fired.
    """
    deadline = started + MARK_CAP * 60
    wanted = set(spawned)
    while True:
        inflight = resolve_inflight(workspace.marks_inflight, workspace.marks_workers)
        if not any(worker.worker in wanted for worker in inflight.running):
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(MARK_POLL)


__all__ = [
    "CLAIM_WAIT",
    "MARK_CAP",
    "MARK_POLL",
    "run_mark",
]
