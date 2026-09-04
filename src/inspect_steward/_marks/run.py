"""The marking runner: one detached process that writes one ruling into its logs.

Spawned by the tend's executor with a run id (`spawn.py`), it reads everything else from the runs record and the journal, does its work, journals the application, and records how it ended. Its posture is a worker's: nothing tends it, it leaves a record of every step so that the next turn can retry exactly what did not land, and a failure costs this run and never the tend.

**The capture guard goes on before anything of the eval's is touched.** Recomputing a log's metrics can import the task's module, and a definition that calls `eval_set()` at module level would then run the eval — here, in a process meant to edit one file. With `INSPECT_EVAL_SET_CAPTURE` set, an inline `eval_set()` enumerates into a scratch path instead, which is the launch's own guard (`_evalset.read`). The fleet strips the variable from the side workers it spawns, so they run.

**A zero is the scorer's verdict on an empty attempt, obtained by running it.** The side run spawns the definition into `.steward/marks/<run>/logs` selecting just the ruled sample ids, watches the worker's control channel, and cancel-scores each sample the moment it is running — so the task's solver stops as it starts and the task's scorer scores what it finds. What the scratch log records for each sample is then copied into the main log's sample by `harvest_scores`, history and provenance intact. Nothing checks that the attempt did no work: an agentic sample takes minutes and is interrupted within a second of starting, and the operator declined a guard for the remainder.

**The main log is read and written under the workspace claim**, both halves inside it. A tend rewrites the same file for a re-run's invalidation or an acceptance, and a log read before the claim and written after it would put back what the tend changed in between.
"""

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from inspect_ai._eval.eval_set_manifest import INSPECT_EVAL_SET_CAPTURE
from inspect_ai._eval.eval_set_overrides import (
    EvalSetOverrides,
    merge_eval_set_overrides,
)
from inspect_ai.log import EvalSample, read_eval_log

from .._anomaly.applied import RULING_APPLIED, read_applied
from .._anomaly.fold import read_anomalies
from .._anomaly.model import Anomalies, Anomaly, Disposition, Ruling
from .._evalset.manifest import ManifestTask, read_manifest, worker_overrides
from .._evalset.observe import observe_logs, observe_tasks
from .._scan import ScanError, initialize_scan
from .._schedule import SpawnTask, SpawnWorker
from .._smoke.run import reap
from .._worker import (
    Fleet,
    Unavailable,
    cancel_sample,
    resolve_eval_set_id,
    resolve_inflight,
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
from .edit import Marked, Target, commit, harvest_scores, mark_unscored
from .state import read_runs, record_exited

MARK_POLL = 0.5
"""Seconds between looks at a side run's control channel.

Short, because the whole point is to reach a sample between its starting and its doing anything: the poll is the ceiling on how much of an attempt the scorer sees.
"""

MARK_CAP = 30
"""Minutes a side run may take before it is reaped and the run fails. A sample cancel-scored as it starts lands in seconds; what this bounds is a worker that never binds a socket or never lands a log."""

CLAIM_WAIT = 120.0
"""Seconds to wait for the workspace claim. A tend holds it for seconds; a launch or a smoke for longer, and those are worth failing this run over rather than editing beside."""

CLAIM_POLL = 2.0


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

    scored: dict[Target, EvalSample] = {}
    side_run: dict[str, Any] | None = None
    if recorded.disposition is Disposition.ZERO:
        scored, side_run = _side_run(workspace, directory, targets)

    by_location: dict[str, list[Target]] = {}
    for target in targets:
        by_location.setdefault(target.location, []).append(target)
    edited: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    with _claim(workspace):
        for location, group in by_location.items():
            log = read_eval_log(location)
            if log.status == "started":
                deferred.extend(
                    _deferral(target, "the log is still being written")
                    for target in group
                )
                continue
            if recorded.disposition is Disposition.EXCLUDE:
                marked = mark_unscored(log, group, anomaly, ruling)
            else:
                marked = harvest_scores(
                    log,
                    {target: scored[target] for target in group if target in scored},
                    anomaly,
                    ruling,
                )
                marked.deferred.extend(
                    (target, "the side run landed no record of it")
                    for target in group
                    if target not in scored
                )
            if marked.edited:
                commit(log, location)
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
    written = sum(len(entry["uuids"]) for entry in edited)
    summary = f"wrote {written} sample{'s' if written != 1 else ''}"
    if deferred:
        reasons = "; ".join(
            f"{entry['id']}:{entry['epoch']} in {entry['task']} ({entry['why']})"
            for entry in deferred
        )
        summary += f", deferred {len(deferred)}: {reasons}"
    return summary


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

    worker: str | None = None


def _side_run(
    workspace: Workspace, directory: Path, targets: Sequence[Target]
) -> tuple[dict[Target, EvalSample], dict[str, Any]]:
    """Score an empty attempt at each target in a scratch run, and read the verdicts back.

    One worker per task, selecting exactly the ruled sample ids and running them all at once, so every target leaves the queue together and the watcher reaches each as it starts. The scratch run is the definition's own — its solver, its limits, its scorer — under `worker_overrides`, exactly as the fleet runs it; only where it writes and which samples it runs differ.

    Returns:
        Each target the side run scored, with the scratch sample carrying that score, and the record the application event carries about the run.
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
    for side in sides.values():
        side.ids = _typed_ids(side.targets)

    log_dir = str(directory / "logs")
    eval_set_id = resolve_eval_set_id(log_dir)
    if manifest.scan is not None:
        # workers require the scan directory to exist and record into it; a
        # fresh id under the scratch directory keeps their rows off the run's
        try:
            initialize_scan(manifest.scan, log_dir=log_dir, scan_id=eval_set_id)
        except ScanError as ex:
            steward_log(workspace.log, f"the side run could not bracket its scan: {ex}")
    definition = Path(manifest.source.path)
    if not definition.is_absolute():
        definition = workspace.root / definition

    spawned: list[str] = []
    started = time.monotonic()
    try:
        for side in sides.values():
            fleet = Fleet(
                definition=definition,
                type=manifest.source.type,
                log_dir=log_dir,
                eval_set_id=eval_set_id,
                workers_dir=workspace.marks_workers,
                inflight=workspace.marks_inflight,
                cwd=workspace.root,
                args=manifest.source.args or None,
                # naming `sample_id` displaces `limit` and `sample_shuffle`,
                # which is upstream's rule and what a slice of named ids wants
                overrides=merge_eval_set_overrides(
                    worker_overrides(manifest),
                    EvalSetOverrides(sample_id=side.ids),
                ),
                scanners=None,
            )
            side.worker = fleet.spawn(
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
            ).worker
            spawned.append(side.worker)
        capped = _watch(workspace, spawned, started)
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

    narrowed = manifest.model_copy(
        update={"tasks": [side.row for side in sides.values()]}
    )
    scored: dict[Target, EvalSample] = {}
    locations: list[str] = []
    for observed in observe_tasks(narrowed, observe_logs(log_dir)).tasks:
        current = observed.current
        side = sides.get(observed.identifier)
        if current is None or side is None:
            continue
        locations.append(current.location)
        log = read_eval_log(current.location)
        by_key = {
            (str(sample.id), sample.epoch): sample for sample in log.samples or []
        }
        for target in side.targets:
            sample = by_key.get((target.sample_id, target.epoch))
            if sample is not None:
                scored[target] = sample
    return scored, {"log_dir": log_dir, "locations": locations}


def _typed_ids(targets: Sequence[Target]) -> list[int | str]:
    """The ids a selection names, typed as the main log records them."""
    wanted = {target.sample_id for target in targets}
    ids: list[int | str] = []
    for location in dict.fromkeys(target.location for target in targets):
        for sample in read_eval_log(location).samples or []:
            if str(sample.id) in wanted and sample.id not in ids:
                ids.append(sample.id)
    return ids


def _watch(workspace: Workspace, spawned: Sequence[str], started: float) -> bool:
    """Cancel-score every sample the side workers start, until they have all gone.

    Each poll reads every live worker's control channel over its own socket — the reads `live.py` makes, made synchronously — and asks `inspect ctl` to end each running sample with `--action score`. A sample not yet running is a 409 and is asked again next poll; one already finished answers `changed: false`. The watch ends when no spawned worker is left in the process table — a worker that has landed its log has exited — or when the cap fires.

    Returns:
        Whether the cap fired.
    """
    deadline = started + MARK_CAP * 60
    asked: set[tuple[str, str, int]] = set()
    wanted = set(spawned)
    while True:
        inflight = resolve_inflight(workspace.marks_inflight, workspace.marks_workers)
        running = [worker for worker in inflight.running if worker.worker in wanted]
        if not running:
            return False
        for worker in running:
            if worker.socket is None:
                continue
            for task_id, eval_id in _live_tasks(worker.socket):
                for sample_id, epoch in _running_samples(worker.socket, eval_id):
                    key = (task_id, sample_id, epoch)
                    if key in asked:
                        continue
                    outcome = cancel_sample(task_id, sample_id, epoch, action="score")
                    if isinstance(outcome, Unavailable):
                        # still initializing, or the worker is busy: next poll
                        continue
                    asked.add(key)
        if time.monotonic() >= deadline:
            return True
        time.sleep(MARK_POLL)


def _live_tasks(socket: Path) -> list[tuple[str, str]]:
    """`(task_id, eval_id)` for every task a worker reports, or nothing where it does not answer."""
    payload = _get(socket, "/tasks")
    if not isinstance(payload, list):
        return []
    found: list[tuple[str, str]] = []
    for row in cast(list[object], payload):
        if not isinstance(row, dict):
            continue
        entry = cast(dict[str, object], row)
        task_id, eval_id = entry.get("task_id"), entry.get("eval_id")
        if isinstance(task_id, str) and task_id and isinstance(eval_id, str):
            found.append((task_id, eval_id))
    return found


def _running_samples(socket: Path, eval_id: str) -> list[tuple[str, int]]:
    """`(sample_id, epoch)` for every running sample of one eval."""
    payload = _get(socket, f"/evals/{eval_id}/samples?all=true")
    if not isinstance(payload, dict):
        return []
    rows = cast(dict[str, object], payload).get("samples")
    if not isinstance(rows, list):
        return []
    found: list[tuple[str, int]] = []
    for row in cast(list[object], rows):
        if not isinstance(row, dict):
            continue
        entry = cast(dict[str, object], row)
        if entry.get("status") != "running":
            continue
        sample_id, epoch = entry.get("sample_id"), entry.get("epoch")
        if sample_id is not None and isinstance(epoch, int):
            found.append((str(sample_id), epoch))
    return found


def _get(socket: Path, path: str) -> object:
    """One read over a worker's socket, or `None` where it did not answer."""
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)),
            base_url="http://localhost",
            timeout=MARK_POLL * 4,
        ) as client:
            response = client.get(path)
            response.raise_for_status()
            return cast(object, response.json())
    except (httpx.HTTPError, OSError, ValueError):
        return None


__all__ = [
    "CLAIM_WAIT",
    "MARK_CAP",
    "MARK_POLL",
    "run_mark",
]
