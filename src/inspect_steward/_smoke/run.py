"""Running the rehearsal: truncated, capped, and answerable to nobody afterwards.

**It commits no desired state, which is the whole of why it is not `launch()`.** No manifest is written, no archive gate is consulted, no delta computed, no timer armed, no `launched` journalled, no tend run. A smoke is a question about a definition, and the answer must not change what the workspace is converging toward — a rehearsal that committed its own truncated shape would leave the next `launch` computing a delta against two samples per task.

**The slice rides the workers and never the capture**, which inverts what `launch` does and is the one non-obvious decision here. Capturing under a `limit` would make the manifest describe the rehearsal: per-task sample counts of two, and a `manifest_digest` differing from the real launch's for the one reason that does not matter. `task_identifier` hashes a task's execution limits and not its dataset slice — verified upstream, and the sentence execution.md §12 item 8 was written for — so a worker asked for two samples is still running the task the capture enumerated. The manifest a smoke captures *is* the manifest the launch will capture, which is what lets the gate compare both the identifiers and the digest instead of guessing from one of them.

**Bounded and untended.** It spawns once, watches, and stops on its deadline; nothing reconciles it in between. That matters for one specific reason: `inspect ctl` cancellation finalizes a log with `status="error"`, which `observe` reads as a task worth retrying — so a capped smoke under a tend would respawn everything it had just stopped. The per-identifier *do not start this again* record that would need is deferred (plan.md, step 16) and stays deferred, because nothing here is tending.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from shutil import rmtree
from typing import Any, cast

from inspect_ai._eval.eval_set_overrides import (
    EvalSetOverrides,
    merge_eval_set_overrides,
)
from inspect_ai.log import EvalLog, read_eval_log

from .._evalset.classify import kind_of, task_error_class
from .._evalset.detect import DefinitionType
from .._evalset.instances import ClassedCache, classed_instances
from .._evalset.manifest import (
    SELECTION,
    Manifest,
    ManifestTask,
    manifest_digest,
    shaping,
    worker_overrides,
)
from .._evalset.observe import ObservedLogs, observe_logs, observe_tasks
from .._launch import LaunchError
from .._launch.launch import (
    capture_run,
    committed_manifest,
    reuse_committed,
    run_overrides,
)
from .._notify import establish_channel
from .._scan import (
    ScanError,
    finalize_scan,
    initialize_scan,
    merged_scanners,
    scan_digest,
    scan_dir_location,
    scan_findings,
    scan_material,
    sync_scan,
)
from .._scan.model import establish_scan_model
from .._schedule import SpawnTask, SpawnWorker
from .._tend.coverage import coverage
from .._tend.detect import scan_attempts
from .._tend.notify import notify_failure
from .._tend.turn import reused_samples
from .._worker import Fleet, resolve_eval_set_id
from .._worker.inflight import RunningWorker, resolve_inflight
from .._worker.stop import StopRequest, stop_workers
from .._workspace import (
    SMOKED,
    Held,
    Workspace,
    acquire,
    append_event,
    read_directives,
    steward_log,
)
from .checks import probe, scan_coverage
from .digest import (
    Outcome,
    Smoke,
    digest_markdown,
    findings,
    journal_fields,
    outcome,
)

DEFAULT_SAMPLES = 2
"""Samples per task a rehearsal runs. Two is not magic; being *bounded* is the point (workflow.md §7.1)."""

DEFAULT_CAP = 15
"""Minutes a rehearsal may take. A Steward-side deadline rather than a `time_limit`, which is part of task identity and is refused by the overrides container outright."""

_POLL = 2.0
"""Seconds between looks at the log directory while the rehearsal runs."""

DRAIN = 60.0
"""Seconds a cancelled worker is given to land what it has.

**A cancel is a request that returns before the work stops.** `inspect ctl` accepts it and comes straight back; the worker then finishes its sample, finalizes its log and writes its scan rows, which takes as long as it takes. Reading the directory in the next breath — as the cap path once did — reads a log still being written and folds a scan still being recorded, so a capped rehearsal reported fewer samples than it actually ran and could prune rows that landed a second later. Bounded rather than open-ended because the alternative to waiting forever is saying so, which the log line at the end of the wait does.
"""


@dataclass(frozen=True)
class Plan:
    """Everything the rehearsal needs, resolved before anything spawns."""

    manifest: Manifest
    log_dir: str
    scan_id: str
    """The eval set id this rehearsal records under — **its own, never the definition's**.

    `scan_dir_location` is `{scans or log_dir}/scan_id={scan_id}`, so a definition that redirects its scans somewhere shared makes `log_dir` irrelevant to where rows land: a smoke under the run's own id resolves to the run's own scan directory, writes rehearsal rows into it, rewrites its summary, and — since the finalize prunes rows naming logs outside the directory it was handed — **prunes the run's rows using the rehearsal's log directory**. Redirecting the smoke's scans locally cannot fix it, because a worker reads `ScannerConfig.scans` out of the definition it executes and would keep writing to the redirect while Steward initialized somewhere else. A distinct id does fix it: every path resolves to `{scans}/scan_id=<this rehearsal>`, which nothing else addresses.

    What remains under a redirect is that the rehearsal's rows land beside the run's rather than inside `.steward/smoke/`. That is residue rather than damage, and it is the definition's own instruction about where scan rows go.
    """

    scan_dir: str | None
    scanners: tuple[str, ...]
    samples: int
    cap: int
    max_workers: int | None = None
    """Processes the rehearsal may divide its tasks into, or `None` for one apiece."""


def prepare(
    workspace: Workspace,
    manifest: Manifest,
    *,
    samples: int = DEFAULT_SAMPLES,
    cap: int = DEFAULT_CAP,
    max_workers: int | None = None,
    scanners: dict[str, dict[str, Any]] | None = None,
) -> Plan:
    """Clear the last rehearsal and bracket this one's scan.

    **Cleared rather than accumulated**, because a rehearsal is disposable by construction: each smoke replaces the previous one, a failed smoke's logs stay for as long as anybody wants to read them, and everything goes when `.steward/` goes. Nothing else needs a cleanup rule.

    **A clearing that fails stops the rehearsal**, which is the one place `ignore_errors` was actively dangerous rather than merely lax. Everything downstream reads this directory as *what this rehearsal produced*: a surviving log from the last one can satisfy `settled` before a worker has written anything, and lands in the digest as this run's evidence. A rehearsal reporting somebody else's results is worse than one that did not run.

    **And a directory the last rehearsal is still using is not cleared at all.** A worker that outlived its drain is still writing a log and still holding sandboxes, and `rmtree` over it takes its in-flight record away from the only thing that could still be asked to stop it — while whatever it writes next lands among this rehearsal's evidence. Nothing tends a smoke, so there is no converging loop to sort that out afterwards; the refusal names the workers and leaves them stoppable.

    The scan is initialized against the *rehearsal's* directory, so the built-in scanner and whatever the definition and `_steward.yaml` add all record into `.steward/smoke/scans/`. `verify_scan` is deliberately not called: it compares a run's scanners against what a previous launch committed for the same directory, and this directory has just been emptied.

    Raises:
        LaunchError: The previous rehearsal is still running, or its directory could not be cleared.
    """
    directory = workspace.smoke
    if lingering := resolve_inflight(
        workspace.smoke_inflight, workspace.smoke_workers
    ).running:
        named = ", ".join(sorted(one.worker for one in lingering))
        raise LaunchError(
            f"the previous rehearsal is still running ({named}) — clearing "
            f"`{directory}` would take its logs and its in-flight record away "
            f"from the only thing that can stop it. Stop it and run this again."
        )
    try:
        if directory.exists():
            rmtree(directory)
    except OSError as ex:
        raise LaunchError(
            f"the previous rehearsal's directory could not be cleared, and "
            f"anything left in it would be read as this one's results: {ex}"
        ) from ex
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "workers").mkdir(exist_ok=True)
    log_dir = str(directory)

    # **a refusal, not a degraded rehearsal, and not a traceback.** A malformed
    # `scanners:` entry or a name colliding with the built-in is a message for a
    # person about a file they just edited -- the same refusal `_launch` turns
    # this into, arriving through the same exception the CLI already prints
    try:
        material = scan_material(manifest.scan, scanners)
    except ScanError as ex:
        raise LaunchError(str(ex)) from ex

    # minted into the freshly cleared directory rather than taken from the
    # manifest, which is what keeps a redirected scan off the run's own rows
    scan_id = resolve_eval_set_id(log_dir)
    scan_dir: str | None = None
    try:
        scan_dir = initialize_scan(material, log_dir=log_dir, scan_id=scan_id)
    except ScanError as ex:
        # reported rather than raised: a rehearsal that cannot bracket its scan
        # is still worth running for everything else it catches, and *the scan
        # could not start* is exactly the kind of thing a smoke exists to say
        steward_log(workspace.log, f"smoke could not initialize scanning: {ex}")

    return Plan(
        manifest=manifest.model_copy(update={"scan": material, "log_dir": log_dir}),
        log_dir=log_dir,
        scan_id=scan_id,
        scan_dir=scan_dir,
        scanners=tuple(sorted(merged_scanners(material))),
        samples=samples,
        cap=cap,
        max_workers=max_workers,
    )


def selection(manifest: Manifest, samples: int) -> dict[str, Any]:
    """The rehearsal's slice, taken **inside** whatever the run already selects.

    **The three selectors move as one, and anything naming one of them takes all three.** That is upstream's rule and it has to be — `eval()` refuses `sample_id` beside either of the others — but it makes the obvious truncation destructive: a bare `limit=2` clears a run's `sample_shuffle` and replaces a `limit` of `(100, 200)`, so the rehearsal runs samples 0 and 1 of a run that never touches them and then records the intended manifest's digest as successfully rehearsed. A rehearsal of different samples, reported as a rehearsal of these ones, is the worst kind of green.

    Read off what is *in force* rather than off the override alone, because a definition calling `eval_set(limit=(100, 200))` records that in `options` with no override at all, and displacing it is the same defect arriving by the other route.

    Every case is representable, which is why this truncates rather than refusing:

    | the run selects | the rehearsal runs |
    | --- | --- |
    | nothing | the first `samples` |
    | `limit=L` | the first `min(samples, L)` |
    | `limit=(a, b)` | `(a, min(a + samples, b))` — the front of the run's own window |
    | `sample_id` | the first `samples` of the ids it names |
    | `sample_shuffle` | the shuffle kept, and the first `samples` of it |

    Args:
        manifest: The captured manifest, whose overrides and options carry the run's selection between them.
        samples: Samples per task the rehearsal may run.

    Returns:
        All three selectors, as `EvalSetOverrides` spells them.
    """
    named: dict[str, Any] = {name: shaping(manifest, name) for name in SELECTION}
    if (ids := named["sample_id"]) is not None:
        # `sample_id` admits neither of the others, so the truncation has to be
        # of the ids themselves. A scalar already names one sample and stands
        chosen = cast(list[Any], ids)[:samples] if isinstance(ids, list) else ids
        return {"sample_id": chosen, "limit": None, "sample_shuffle": None}

    limit = named["limit"]
    window: int | tuple[int, int]
    if isinstance(limit, (list, tuple)):
        # a manifest read back off disk spells a range as a list, and a live
        # container spells it as a tuple. Both are the same instruction
        bounds = [int(one) for one in cast(Sequence[Any], limit)]
        start, stop = bounds[0], bounds[1]
        window = (start, min(start + samples, stop))
    elif isinstance(limit, int):
        window = min(samples, limit)
    else:
        window = samples
    # the shuffle is kept rather than dropped: which samples the front of a
    # shuffled dataset holds is part of what the run is, so a rehearsal of the
    # unshuffled front is a rehearsal of different samples
    return {
        "limit": window,
        "sample_id": None,
        "sample_shuffle": named["sample_shuffle"],
    }


def overrides(plan: Plan, base: EvalSetOverrides | None) -> EvalSetOverrides:
    """What every rehearsal worker is told, on top of the run's own overrides.

    Three things, and each is here for its own reason. `log_dir` sends the logs somewhere they cannot be mistaken for results. The selection is the truncation — a *slice*, not `max_samples`, which is concurrency and bounds how many run at once rather than how many run at all — and it is computed inside the run's own by `selection`, since naming any selector displaces all three. And `log_model_api` keeps every raw provider call rather than the first five per model, which is what makes the reasoning check answerable past the fifth turn; the rehearsal's logs are thrown away, so the size costs nothing.
    """
    mine = EvalSetOverrides(
        log_dir=plan.log_dir,
        log_model_api=True,
        **selection(plan.manifest, plan.samples),
    )
    # the merge only returns nothing when both sides are nothing, and one side
    # here is always this rehearsal's own three fields
    return merge_eval_set_overrides(base, mine) or mine


def divide(plan: Plan) -> list[tuple[SpawnTask, ...]]:
    """Deal the rehearsal's tasks into the processes it is allowed.

    **`max_workers` is the run's shape and a rehearsal has to have it too.** Left out, a fifty-task definition constrained to four processes rehearsed in fifty — which is not the run, is a burst of startup cost the operator had already declined, and on a machine picked for four is the rehearsal failing for a reason the launch would not have hit. Nothing else about the fleet is borrowed: `max_tasks` bounds how much runs at once and a rehearsal is already bounded, by `samples` and by the cap.

    Dealt round-robin rather than sliced, which is `_pour`'s reasoning one layer down: consecutive tasks are the same task across models, so a contiguous slice puts every arm of one task in one process and loses them together.
    """
    tasks = [
        SpawnTask(
            identifier=task.identifier,
            key=task.key,
            resume=None,
            attempt=1,
            reason=None,
            registry_name=task.registry_name,
            args_hash=task.args_hash,
        )
        for task in plan.manifest.tasks
    ]
    if not tasks:
        return []
    processes = (
        len(tasks) if plan.max_workers is None else min(plan.max_workers, len(tasks))
    )
    dealt: list[list[SpawnTask]] = [[] for _ in range(max(1, processes))]
    for index, task in enumerate(tasks):
        dealt[index % len(dealt)].append(task)
    return [tuple(share) for share in dealt]


def spawn(
    workspace: Workspace, definition: Path, plan: Plan, *, deadline: float | None = None
) -> list[str]:
    """Start the rehearsal's workers, and never look at scheduling again.

    Deliberately not `reconcile`: converging is what a tend does, and a rehearsal that respawned a failed task would be answering a different question — *does this work if you try it twice* — while spending the budget the cap exists to bound.

    **A spawn that fails halfway takes back what it started.** Nothing tends a rehearsal, so a worker left behind by an exception on the third task of five is a worker nobody will ever stop: it holds sandboxes and spends tokens against a smoke that has already reported. Only this function knows what it started when it failed, which is why the cleanup is here rather than in the caller's `finally`.

    **The deadline is checked between spawns**, because starting a worker is not free and a wide fleet can spend the whole cap getting up. Stopping leaves those tasks with no log, which `conclude` reports and the cap already accounts for — the rehearsal ends as `capped`, which is the true answer.

    Args:
        workspace: The workspace being rehearsed in.
        definition: The definition every worker runs.
        plan: The rehearsal.
        deadline: Monotonic time the cap fires at, or `None` for no deadline.

    Returns:
        The workers started, in order.
    """
    fleet = Fleet(
        definition=definition.resolve(),
        type=plan.manifest.source.type,
        log_dir=plan.log_dir,
        eval_set_id=plan.scan_id,
        # **the rehearsal's own, never the run's.** `resolve_inflight` counts
        # spent attempts per identifier and `reconcile` stops respawning after
        # two, so a smoke writing here would spend the run's attempt budget on
        # rehearsals -- measured: two smokes left every task stalled before the
        # real launch ran a sample
        workers_dir=workspace.smoke_workers,
        inflight=workspace.smoke_inflight,
        cwd=workspace.root,
        args=plan.manifest.source.args or None,
        # **`worker_overrides` and not the raw ones**, which is the difference
        # between rehearsing the run and rehearsing something else: the real
        # fleet gets `DEFAULT_RETRY_ON_ERROR` where nobody asked for a number,
        # so a rehearsal on the raw overrides ran at no retries at all -- a
        # transient blip errored a sample the launch would have retried, which
        # now fails the rehearsal, and the retry path itself went unexercised
        overrides=overrides(plan, worker_overrides(plan.manifest)),
        scanners=(
            plan.manifest.scan.injected if plan.manifest.scan is not None else None
        ),
    )
    spawned: list[str] = []
    try:
        for share in divide(plan):
            if deadline is not None and time.monotonic() >= deadline:
                steward_log(
                    workspace.log,
                    f"the smoke's cap fired while its fleet was still starting; "
                    f"{len(spawned)} worker(s) had started",
                )
                break
            spawned.append(
                fleet.spawn(SpawnWorker(tasks=share, max_samples=plan.samples)).worker
            )
    except BaseException:
        reap(workspace, spawned)
        raise
    return spawned


def settled(logs: ObservedLogs, manifest: Manifest) -> bool:
    """Whether every task has landed a finished log.

    Read off the directory rather than off the process table, on the discipline the rest of Steward keeps: a worker that exited without landing anything is not a task that finished, and the log is the artifact.
    """
    observed = observe_tasks(manifest, logs)
    return all(
        task.current is not None and task.current.status != "started"
        for task in observed.tasks
    )


def watch(plan: Plan, *, now: float) -> bool:
    """Watch until every task settles or the deadline fires.

    **It observes and nothing else.** Stopping what is left belongs to `reap`, which the caller runs from a `finally` — so the cap, an exception, and a `Ctrl-C` all leave by the same door, and there is one place that knows how to take a fleet down.

    Args:
        plan: The rehearsal.
        now: The monotonic clock reading the cap is measured from.

    Returns:
        Whether the cap fired.
    """
    deadline = now + plan.cap * 60 if plan.cap else None
    while True:
        if settled(observe_logs(plan.log_dir), plan.manifest):
            return False
        if deadline is not None and time.monotonic() >= deadline:
            return True
        time.sleep(_POLL)


def reap(workspace: Workspace, workers: Sequence[str]) -> list[str]:
    """Take down whatever of this rehearsal is still running, and wait for it to land.

    **The one exit, taken from a `finally`.** A rehearsal is untended by design, which is the property that makes it cheap and also the one that makes a leaked worker permanent: nothing will ever reconcile it, so a worker outliving the smoke keeps its sandboxes and keeps spending until somebody notices by hand. The cap is the expected way to get here; an exception, a claim that could not be released, and an interrupt are the ones that made this a `finally`.

    Cancelling keeps every completed sample and finalizes the log, which is what makes a capped rehearsal still worth reading — it is a partial answer rather than none. The `status="error"` those logs land with is nobody's problem here, because nothing is going to reconcile them.

    **Then it waits**, which is the half that was missing. A cancel returns as soon as it is accepted, so the caller that read the directory next would read logs still being finalized and fold scan rows still being written. Bounded at `DRAIN`; a worker still there afterwards is *returned* rather than escalated, because the alternative — signalling a process mid-write — discards exactly the partial answer this path exists to keep.

    **And what it returns is a failure, which is the half that was missing after that.** A drain that timed out used to log a line and let the digest conclude whatever it liked: the tasks had settled, so the cap never fired, and a rehearsal reported *ready* with one of its own workers still generating. That worker outlives the smoke with nothing to reconcile it, and the next rehearsal's `rmtree` takes its logs and its in-flight record out from under it — which is why `prepare` now refuses that directory rather than clearing it.

    Returns:
        The workers still running when the drain gave up, empty where everything landed.
    """
    if not workers:
        return []
    wanted = set(workers)
    if not (running := _running(workspace, wanted)):
        return []

    requests = [
        StopRequest(worker=worker, identifiers=worker.identifiers) for worker in running
    ]
    for stopped in stop_workers(requests):
        if not stopped.graceful:
            steward_log(
                workspace.log,
                f"smoke worker {stopped.worker} was {stopped.outcome.value}: "
                f"{stopped.detail}",
            )

    deadline = time.monotonic() + DRAIN
    while left := _running(workspace, wanted):
        if time.monotonic() >= deadline:
            steward_log(
                workspace.log,
                f"{len(left)} smoke worker(s) were still finishing {DRAIN:.0f}s "
                f"after being cancelled; the digest may under-report what ran",
            )
            return [worker.worker for worker in left]
        time.sleep(_POLL)
    return []


def _running(workspace: Workspace, wanted: set[str]) -> list[RunningWorker]:
    """This rehearsal's workers that the process table still confirms."""
    inflight = resolve_inflight(workspace.smoke_inflight, workspace.smoke_workers)
    return [worker for worker in inflight.running if worker.worker in wanted]


def read(logs: ObservedLogs) -> tuple[list[EvalLog], list[str]]:
    """Read the rehearsal's logs whole — the one read Steward makes at this depth.

    Bounded by the rehearsal's own slice and confined to `.steward/smoke/` by the path it is handed, which is what makes it the named exception rather than a hole in the discipline (`checks.py`).
    """
    read_logs: list[EvalLog] = []
    errors: list[str] = []
    for attempts in logs.attempts.values():
        for attempt in attempts:
            try:
                read_logs.append(read_eval_log(attempt.location))
            except Exception as ex:
                errors.append(f"could not read {Path(attempt.location).name}: {ex}")
    return read_logs, errors


@dataclass(frozen=True)
class Folded:
    """What the rehearsal's own scan came to."""

    findings: tuple[str, ...] = ()
    """One sentence per class, in the tend's wording."""

    threw: int = 0
    """Transcripts a scanner threw on.

    **Blocking, and it was not.** These arrive as `scanerror:` classes among the findings, which are reported and count toward nothing — so every scanner in the run could fail on every transcript while the digest read *rehearsed and ready* and the journal recorded a pass. During a run that class is a question for a person, because the samples are fine and only the reading of them failed; before one it is the scan path telling you it does not work, which is among the things workflow.md §7.1 says a rehearsal is for.
    """

    reviewed: int = 0
    """Transcripts every scanner answered for.

    **The number a census of findings cannot supply**, and without it a scan that recorded nothing is indistinguishable from a scan that found nothing: no findings, no errors, nothing thrown, and a verdict of *rehearsed and ready* over a scan path that never wrote a row. Counted the way the tend counts it, through `coverage`, so the rehearsal and the run it precedes mean the same thing by *reviewed*.
    """

    landed: int = 0
    """Samples those transcripts are out of, over the tasks a count could be taken for."""

    scanning: bool = False
    """Whether this rehearsal had any scan material to review with. `False` makes the coverage check *unexercised* rather than failed — a run that scans nothing has no scan path to rehearse."""

    errors: list[str] = field(default_factory=list[str])


def fold(plan: Plan, logs: ObservedLogs) -> Folded:
    """Compact the rehearsal's scan rows, read what the scanners said, and count what they reached.

    **Finalized rather than merely synced**, which is what a terminal scan wants and what no other non-signoff caller does: the rehearsal is over the moment this runs, so pruning orphans and marking the scan complete is simply true here. It also exercises the finalize path itself, which is one more thing a rehearsal is for.

    **Coverage is folded here and not left to the findings**, because the two answer different questions and only one of them is *did the scanners run*. Through the tend's own `coverage` — with no `reused` and nothing `unverified`, since a rehearsal spawns once and never resumes, which is the one shape that function's three states exist to separate.
    """
    if plan.scan_dir is None:
        return Folded(errors=["scanning never started, so nothing was reviewed"])
    errors: list[str] = []
    scan_id = plan.scan_id
    scans = plan.manifest.scan.scans if plan.manifest.scan is not None else None
    try:
        sync_scan(log_dir=plan.log_dir, scan_id=scan_id, scans=scans)
        finalize_scan(log_dir=plan.log_dir, scan_id=scan_id, scans=scans)
    except Exception as ex:
        errors.append(f"the scan would not fold: {ex}")
    directory = scan_dir_location(log_dir=plan.log_dir, scan_id=scan_id, scans=scans)
    observed = observe_tasks(plan.manifest, logs)
    found = scan_findings(
        directory,
        scanners=plan.scanners,
        attempts=scan_attempts(observed, logs),
    )
    errors.extend(
        f"{one.what} {one.location}: {one.reason}" for one in found.unreadable
    )
    # **`reused` rather than an assertion that a rehearsal never resumes.** It
    # spawns once and respawns nothing, but `eval_set()` retries a failed task
    # inside the worker -- and for a task with two logs the recorded rows are
    # the union across both, which reads as full coverage over transcripts the
    # current log replaced
    reused, unverified = reused_samples(observed, found)
    reached = coverage(
        observed,
        found.recorded,
        reused=reused,
        unverified=unverified,
        scanning=True,
    )
    return Folded(
        findings=findings(found.instances),
        threw=sum(
            1 for one in found.instances if kind_of(one.class_key) == "scanerror"
        ),
        reviewed=reached.scanned,
        landed=reached.landed,
        scanning=True,
        errors=errors,
    )


def expected(task: ManifestTask, samples: int) -> int:
    """Sample records the rehearsal's slice asks of one task.

    **`min(samples, task.samples) × epochs`, and it is that simple for one reason:** the capture is untruncated, so `task.samples` is what the *run* selects, and `selection` always takes at most `samples` from inside that. Every branch collapses here — the front of a `(100, 200)` window, the first of a set of named ids, the front of a kept shuffle, or the whole of a task with fewer samples than the rehearsal asked for. Epochs multiply rather than truncate, because a slice cuts the dataset and not the repeats.

    Args:
        task: The task, as the untruncated capture enumerated it.
        samples: Samples per task the rehearsal may run.
    """
    return min(samples, task.samples) * task.epochs


def unfinished(manifest: Manifest, logs: ObservedLogs) -> list[str]:
    """Every task that did not come back with a good log, and what happened to it.

    **`settled` and *finished well* are different questions, and reading one for the other made a failed rehearsal pass.** `settled` asks whether the watch can stop, so a log finalized `error` or `cancelled` satisfies it — that is the whole point, since a task that died is not a task to keep waiting for. But a task-level failure lands no errored samples to class: a definition that will not import, an OOM, a scorer that throws in `Task` construction all produce a log with an exception in its header and nothing underneath, so the sample census sees a clean run of zero samples and the rehearsal reported ready. Reproduced.

    Classed with `task_error_class`, which is what the tend keys a failed attempt on, so a rehearsal and the run it precedes name the same failure the same way.
    """
    lines: list[str] = []
    for task in observe_tasks(manifest, logs).tasks:
        current = task.current
        if current is None:
            lines.append(f"{task.key} produced no log")
        elif current.status == "started":
            lines.append(f"{task.key} did not finish")
        elif current.status != "success":
            classed = task_error_class(current.error, current.error_traceback)
            lines.append(f"{task.key} {current.status} — {classed}")
    return lines


def short_slices(
    plan: Plan, logs: ObservedLogs, read_logs: Sequence[EvalLog]
) -> list[str]:
    """Every task that finished cleanly holding less than the slice asked for.

    **Finished is not *ran what was asked*, and that gap is the same defect `unfinished` closes one layer in.** A task whose log finalizes `success` carrying one of the two samples the rehearsal named lost a sample somewhere that left no error behind — a dataset shorter than the capture says, a sample filtered out downstream, a worker that stopped early and finalized cleanly. Nothing else notices: the status is good, no sample errored, and every count downstream reads the smaller number as the whole truth. Reproduced: a two-sample task with a successful one-sample log passed at `landed=1` against a population of 2.

    **Counted off the records rather than off the header**, which are two different claims. `total_samples` is what a log says about itself; the smoke has already read these logs whole for the checks, so the honest numerator is in hand and is also exactly the number the digest reports as landed. A document saying *3 samples* in one line and *expected 4* in another is only useful if both came from the same count.

    Args:
        plan: The rehearsal, for the slice it asked for.
        logs: Its log directory, observed — the map from task to the files it produced.
        read_logs: Those files, read.

    Returns:
        One line per task that came up short.
    """
    records = current_records(plan.manifest, logs, read_logs)
    lines: list[str] = []
    for task in plan.manifest.tasks:
        landed = records.get(task.identifier)
        if landed is None:
            # a task with no log at all is `unfinished`'s to name, and naming it
            # twice in two vocabularies is one failure reported as two
            continue
        if landed < (wanted := expected(task, plan.samples)):
            lines.append(
                f"{task.key} landed {landed} of the {wanted} samples the "
                f"rehearsal asked for"
            )
    return lines


def current_records(
    manifest: Manifest, logs: ObservedLogs, read_logs: Sequence[EvalLog]
) -> dict[str, int]:
    """Per task, the sample records its **current** log holds.

    **Summed across attempts, two halves add up to a whole that does not exist.** A rehearsal is a one-shot, but `eval_set()` retries a failed task inside the worker, so a task can land two logs — and a completeness check over their union let two one-record attempts satisfy a two-record slice while the log that is actually the result held one. Every downstream number has the same shape: what the run *is* is the current attempt, and the history is a different question that `failures` answers separately — an error that happened, happened, whether or not a retry papered over it.

    A task whose current log could not be read is absent rather than zero, on the discipline `coverage` keeps: *nothing is known* and *nothing is there* send a reader after two different problems.

    Args:
        manifest: The captured manifest.
        logs: The rehearsal's log directory, observed — which attempt is current is its answer.
        read_logs: Those logs, read whole.

    Returns:
        Task identifier to record count, for every task whose current log was read.
    """
    records = {log.location: len(log.samples or []) for log in read_logs}
    landed: dict[str, int] = {}
    for task in observe_tasks(manifest, logs).tasks:
        current = task.current
        if current is not None and current.location in records:
            landed[task.identifier] = records[current.location]
    return landed


def failures(logs: ObservedLogs) -> tuple[tuple[str, ...], int]:
    """What the rehearsal's samples did, classed the way a tend classes them.

    **The half of a rehearsal a settled task hides.** Workers force `continue_on_fail`, so a task whose every sample errored still finishes `success` and lands a log — which means *did the tasks finish* answers none of what a smoke is for. A wrong key, a sandbox image that will not start, a scorer that throws: each of them is a green exit over a set of errored samples, and each is exactly what somebody runs a rehearsal to find out before committing a night to it.

    Through `classed_instances` rather than a reading of its own, so the sentence a smoke prints about a failure and the sentence the tend prints about the same failure three hours later are the same sentence. It reads summaries, never transcripts.

    Returns:
        One line per class — errored samples and operator-terminated ones alike — and the number of *errored* samples, which is the count that fails the rehearsal. A sample stopped by a message or token limit ran as designed and is reported without blocking.
    """
    classed = classed_instances(logs, errored_running=set(), cache=ClassedCache())
    errored = sum(1 for one in classed.instances if kind_of(one.class_key) == "error")
    return findings(classed.instances), errored


def models(manifest: Manifest, *, scan_model: str | None = None) -> list[str]:
    """Every model this run puts under load, including the one that reviews it.

    **Role models count.** A task's `model_roles` names the graders, the critics and the attackers a definition wires up beside the model under evaluation, and each of them generates against a context window like any other. A grader silently running at an assumed 128000 is the same failure as the eval doing it, arriving somewhere nobody thinks to look — and a definition where the interesting model *is* a role would have been checked only on its main one.

    The scan model likewise: a scanner reviewing transcripts through a mis-resolved window is the same failure one layer over, and the half nobody would think to check by hand.
    """
    named = {task.model for task in manifest.tasks if task.model}
    named.update(
        role
        for task in manifest.tasks
        for role in (task.model_roles or {}).values()
        if role
    )
    if scan_model:
        named.add(scan_model)
    return sorted(named)


def conclude(
    plan: Plan,
    *,
    logs: ObservedLogs,
    capped: bool,
    elapsed: float,
    waived: Sequence[str],
    scan_model: str | None = None,
    lingering: Sequence[str] = (),
) -> Smoke:
    """Turn a finished rehearsal into the digest that gates the launch.

    Args:
        plan: The rehearsal.
        logs: Its log directory, observed after the reap.
        capped: Whether the deadline fired before the tasks settled.
        elapsed: Wall clock the rehearsal took.
        waived: Check names `--accept` asked for.
        scan_model: The model the scanners reviewed with, as the fleet had it.
        lingering: Workers still running when the drain gave up. A rehearsal cannot report *ready* while one of its own processes is still generating against the account the run is about to use.
    """
    read_logs, errors = read(logs)
    if not plan.manifest.tasks:
        # **an empty capture settles instantly and establishes nothing.** Every
        # `all(...)` over no tasks is true, so the watch returns at once, no
        # worker starts, every check reads *unexercised*, and the journal
        # records a passing smoke at `tasks=landed=population=0` -- blessing a
        # definition edited to nothing, or an argument that filtered every task
        # away, as rehearsed and ready
        errors.append(
            "the capture enumerated no tasks, so this rehearsal ran nothing "
            "and established nothing"
        )
    scan = fold(plan, logs)
    errors.extend(scan.errors)
    errors.extend(unfinished(plan.manifest, logs))
    errors.extend(short_slices(plan, logs, read_logs))
    errors.extend(
        f"smoke worker {one} was still running after being cancelled"
        for one in lingering
    )
    sample_failures, errored = failures(logs)
    result = probe(read_logs, models=models(plan.manifest, scan_model=scan_model))
    # spliced rather than computed inside `probe`, which is about transcripts and
    # has no scan fold to ask -- what it buys is the whole of the check
    # machinery: a waiver by name, a mark in the digest, and a place in the verdict
    result = replace(
        result,
        checks=result.checks
        + (
            scan_coverage(
                reviewed=scan.reviewed, landed=scan.landed, scanning=scan.scanning
            ),
        ),
    )
    # the current attempts rather than every file in the directory, so this
    # number, the slice check and the coverage denominator are all over the
    # same population -- a retry's superseded log is history, not results
    landed = sum(current_records(plan.manifest, logs, read_logs).values())
    return Smoke(
        outcome=outcome(
            result,
            waived=waived,
            capped=capped,
            errors=len(errors),
            errored=errored,
            threw=scan.threw,
        ),
        identifiers=tuple(task.identifier for task in plan.manifest.tasks),
        # the `scan` and `log_dir` this manifest was copied with are not hashed,
        # so this is the digest the launch will compute for the same definition
        digest=manifest_digest(plan.manifest),
        # and because it is not hashed, it is recorded beside it: the scan
        # configuration is the one part of what a rehearsal exercised that a
        # manifest digest cannot speak for, and a *name* is not it
        scanners=scan_digest(plan.manifest.scan),
        scan_model=scan_model or "",
        probe=result,
        waived=tuple(waived),
        tasks=len(plan.manifest.tasks),
        landed=landed,
        # `samples × epochs`, which is `observe`'s own `required_samples` and
        # has to be: the numerator counts sample *records* off the logs, and a
        # two-epoch task contributes two of them per dataset row
        population=sum(task.samples * task.epochs for task in plan.manifest.tasks),
        findings=scan.findings,
        failures=sample_failures,
        errored=errored,
        threw=scan.threw,
        errors=tuple(errors),
        elapsed=elapsed,
        samples=plan.samples,
        cap=plan.cap,
        log_dir=plan.log_dir,
    )


def smoke(
    workspace: Workspace,
    definition: Path,
    *,
    args: dict[str, Any] | None = None,
    type: DefinitionType | None = None,
    samples: int = DEFAULT_SAMPLES,
    cap: int = DEFAULT_CAP,
    accept: Sequence[str] = (),
    overrides: dict[str, Any] | None = None,
    max_workers: int | None = None,
    notification: str | bool | None = None,
    scan_model: str | bool | None = None,
    break_stale: bool = True,
) -> Smoke | Held:
    """Rehearse the definition, and say whether the run it precedes is ready.

    **No credentials pre-check, unlike a launch, and the difference is what that check is about.** `launch` refuses when a credential is set in this shell and absent from `.env`, because a *scheduled tend* inherits a stripped environment and would fail every fire at 02:00 with nobody watching. A smoke arms no timer and spawns its workers from this process, so they inherit exactly what the person at the terminal has. A missing key is still caught — by the rehearsal failing, which is one of the things workflow.md §7.1 says it is for — rather than by a question about a scheduler that is not going to exist.

    **The claim is held for the whole rehearsal**, which is the one place a smoke is expensive in a way a launch is not: fifteen minutes is a long time to hold a workspace. It is correct anyway — a tend converging the real run while a rehearsal spawns workers into the same workspace would have two writers of `inflight.jsonl` and one process table nobody can read cleanly — and it is bounded by the cap, which is the argument for the cap having a default at all.

    **It captures through `capture_run`, which is what makes it a rehearsal of the launch rather than of the definition.** A workspace launched once with `-A` and `--epochs` has those in its committed manifest, and a later bare `launch` reuses them; a rehearsal that captured the definition's own defaults instead would establish that a run nobody is about to make works, and the gate would then compare identifiers across two different eval sets and find them fine.

    **The channel and the scan model are settled before the first worker starts**, for the reason a tend settles them in its first two lines: both reach the fleet through this process's environment, so establishing them afterwards leaves the workers scanning with the shell's answer and unable to reach anybody, while the digest reports the configured one. A rehearsal that blessed a scan model it never exercised is worse than one that never looked.

    Args:
        workspace: The workspace to rehearse in.
        definition: The definition to read. Arguments, type and overrides not given here are reused from the committed manifest, exactly as a re-launch reuses them.
        args: Definition arguments, as `launch` takes them. `None` reuses the committed manifest's; an empty mapping asks for the definition's own.
        type: Definition type, or `None` to reuse then detect.
        samples: Samples per task.
        cap: Wall-clock minutes, or `0` for no deadline.
        accept: Checks to record as waived rather than treat as failures.
        overrides: Inspect's own eval-set arguments for the *run* — the rehearsal adds its slice on top, per worker. `None` reuses the committed manifest's; an empty mapping is `--no-overrides` and displaces the environment too.
        max_workers: Processes to divide the tasks into, or `None` for what the workspace says and then one apiece.
        notification: Where to post, `False` for nowhere, or `None` for what the workspace says. Reaches the rehearsal's workers, whose own notifications are blocking prompts.
        scan_model: The model scanners use, `False` for none, or `None` for what the workspace says.
        break_stale: Whether to break a claim whose holder is gone.

    Returns:
        What the rehearsal established, or the claim that stopped it starting.
    """
    directives = read_directives(workspace.directives)
    inspect_overrides = run_overrides(overrides)

    outcome = acquire(
        workspace.claim, command="launch --smoke", break_stale=break_stale
    )
    if isinstance(outcome, Held):
        return outcome

    with outcome:
        manifest = capture_run(
            workspace,
            definition,
            committed_manifest(workspace),
            args=args,
            type=type,
            overrides=inspect_overrides,
            reuse_overrides=reuse_committed(overrides, inspect_overrides),
        )
        plan = prepare(
            workspace,
            manifest,
            samples=samples,
            cap=cap,
            max_workers=(
                max_workers if max_workers is not None else directives.max_workers
            ),
            scanners=directives.scanners,
        )
        establish_channel(
            workspace,
            directives,
            notification=notification,
            fleet=directives.notification,
        )
        reviewer = establish_scan_model(
            scan_model if scan_model is not None else directives.scan_model
        )

        started = time.monotonic()
        deadline = started + cap * 60 if cap else None
        workers: list[str] = []
        lingering: list[str] = []
        try:
            workers = spawn(workspace, definition, plan, deadline=deadline)
            capped = watch(plan, now=started)
        finally:
            lingering = reap(workspace, workers)

        result = conclude(
            plan,
            # read after the reap rather than before it, so a capped rehearsal
            # reports the logs its workers actually landed
            logs=observe_logs(plan.log_dir),
            capped=capped,
            elapsed=time.monotonic() - started,
            waived=accept,
            scan_model=reviewer,
            lingering=lingering,
        )
        return _record(workspace, result, notification=notification)


def _record(
    workspace: Workspace, result: Smoke, *, notification: str | bool | None
) -> Smoke:
    """Write the digest down, journal what it concluded, and say so if it failed.

    **The notification is not optional and not the agent's.** Before the first worker of the *real* run starts, silence is total: a launch blocked here has no tend, no `status.md`, and nothing posting at all, so a failed gate does not announce itself (agent.md §7). This is the second `stopped` Steward sends without an agent present, on exactly the reasoning the parked-worker one already carries — nothing progresses until a person answers, and nobody else is going to say so.

    **A rehearsal nobody can read the result of has not passed.** Both writes used to be swallowed into `steward.log`, which is the wrong reading of *best effort* for these two in particular: the digest is one of the four artifacts agent.md §9 tells an agent to trust over an exit code, and the journal is what a later `launch` reads to decide the run is rehearsed. Lose the first and keep the second and the gate says *rehearsed* while the terminal points at a file that is not there. So a write that fails is an error of the rehearsal, the verdict is recomputed over it, and the journal records **that** — which also invalidates any older pass, since the newest smoke is the answer whatever it concluded.

    Args:
        workspace: The workspace rehearsed.
        result: What the rehearsal established.
        notification: The channel the operator named, `False` for nowhere, or `None` for the workspace's own. Handed on rather than reduced to a yes: `notify_failure` re-reads the file, so a rehearsal that only said *there was a channel* posted its failure to the workspace's target while the operator watched the one they had just named on the command line. `--no-notification` silences Steward here and never the fleet, which is why the workers were given the workspace's channel regardless.

    Returns:
        The result as recorded, which is the argument unless writing it down failed.
    """
    errors = list(result.errors)
    if (failure := _write_digest(workspace, result)) is not None:
        errors.append(failure)
    final = _amended(result, errors)

    try:
        append_event(workspace.journal, SMOKED, **journal_fields(final))
    except OSError as ex:
        errors.append(f"the rehearsal could not be journalled: {ex}")
        final = _amended(result, errors)
        # **and the correction is appended, not merely returned.** The append
        # is a write followed by an `fsync`, so a failure does not mean the
        # line is absent: an `fsync` that failed after the write landed leaves
        # a journal whose newest smoke says `passed`, and `read_smoked` takes
        # the newest whatever it concluded -- so the next launch reads a pass
        # nobody is returning. A second event settles it in the safe direction
        # for both shapes of failure: where the first line landed this one
        # supersedes it, and where it did not this is the only record
        try:
            append_event(workspace.journal, SMOKED, **journal_fields(final))
        except OSError as second:
            steward_log(
                workspace.log,
                f"the smoke could not be journalled ({ex}), and neither could "
                f"the correction ({second}); a later launch may read an older "
                f"rehearsal as current",
            )
        # the file on disk now disagrees with what is being returned, so it is
        # written again -- once, best effort, and already carrying the failure
        # that the first write did not know about
        _write_digest(workspace, final)

    if not final.passed:
        notify_failure(workspace, _verdict_line(final), notification=notification)
    return final


def _write_digest(workspace: Workspace, result: Smoke) -> str | None:
    """Write the digest, returning what went wrong if anything did."""
    digest = workspace.smoke / "digest.md"
    try:
        digest.write_text(digest_markdown(result), encoding="utf-8", newline="")
    except OSError as ex:
        steward_log(workspace.log, f"could not write {digest}: {ex}")
        return f"the digest could not be written to {digest}: {ex}"
    return None


def _amended(result: Smoke, errors: Sequence[str]) -> Smoke:
    """The same rehearsal with what went wrong recording it, and the verdict redone.

    A cap stays a cap: nothing was established either way, and a write that also failed does not change which question is open.
    """
    if tuple(errors) == result.errors:
        return result
    return replace(
        result,
        errors=tuple(errors),
        outcome=outcome(
            result.probe,
            waived=result.waived,
            capped=result.outcome is Outcome.CAPPED,
            errors=len(errors),
            errored=result.errored,
            threw=result.threw,
        ),
    )


def _verdict_line(result: Smoke) -> str:
    """What the post says, which is the digest's verdict and where to read the rest."""
    blocked = ", ".join(result.blocked) or str(result.outcome)
    return (
        f"the smoke did not pass ({blocked}) — nothing has been launched; "
        f"read {result.log_dir}/digest.md"
    )


__all__ = [
    "DEFAULT_CAP",
    "DEFAULT_SAMPLES",
    "Outcome",
    "Plan",
    "conclude",
    "current_records",
    "divide",
    "expected",
    "failures",
    "fold",
    "models",
    "overrides",
    "prepare",
    "read",
    "reap",
    "selection",
    "settled",
    "short_slices",
    "smoke",
    "spawn",
    "unfinished",
    "watch",
]
