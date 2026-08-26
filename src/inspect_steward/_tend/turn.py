"""The turn: observe, decide, act, record.

Everything below this composes; nothing below this composes anything. `reconcile` decides, `observe_*` reads, `Fleet.spawn` acts, the journal records — and until now nothing called them together. This is that call, and it is deliberately the only place in the package that knows about all four at once, which is why it lives above them rather than inside `_schedule` (whose purity is load-bearing, and which `_workspace` already imports).

**Two dispositions of one function.** `tend` executes the actions; `status` computes them and throws them away.

| verb | actions | claim | writes |
|---|---|---|---|
| `status` | computed, **discarded** | reported, never taken | nothing at all |
| `tend` | computed, **executed** | held for the seconds it runs | journal, `status.md`, the log directory |

They cannot drift, because they are the same code path with one flag. That is worth more than the duplication it saves: a preview that disagrees with what the next turn actually does is worse than no preview.

**`status` must stay read-only, because every convention in the ecosystem promises it is.** `git status`, `systemctl status`, `docker ps` — somebody typing `steward status` to satisfy their curiosity about an overnight sweep must not thereby launch eight workers. Surprise as a side effect of looking is the one thing a runner of expensive jobs cannot afford. It does not even write `steward.log`, and the cost of that (a failed status leaves no trace) is smaller than the cost of a read verb that writes.

**A turn never blocks.** Everything with an unbounded duration is a detached child that some later turn observes finishing. That is what keeps the claim short-lived, which is in turn what makes a claim older than a generous threshold unambiguously stale — the property the whole driver model rests on (execution.md, *A scan is a detached process, not part of a tend*).

**An interrupted turn is reconciled by the next one**, and that is a requirement rather than a hope. There is no resume path here and no partial-turn state to recover: the next turn re-reads the log directory and the process table and decides again from what it finds. Every write is ordered so that being interrupted before it costs a repeat rather than a corruption.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .._evalset.archive import archive_log
from .._evalset.cache import read_attempt_cache, write_attempt_cache
from .._evalset.manifest import (
    Manifest,
    definition_hash,
    read_manifest,
)
from .._evalset.observe import observe_logs, observe_tasks
from .._schedule import (
    Action,
    ArchiveLog,
    InFlight,
    Pool,
    ReapWorker,
    SpawnWorker,
    Summary,
    reconcile,
)
from .._worker import (
    Fleet,
    LiveFleet,
    LiveTarget,
    read_fleet,
    record_exited,
    resolve_eval_set_id,
    resolve_inflight,
)
from .._workspace import (
    DirectivesError,
    Held,
    Workspace,
    acquire,
    append_event,
    read_claim,
    read_directives,
    read_journal,
    resolve_pool,
    steward_log,
)
from .progress import Progress, task_progress
from .render import status_markdown

OBSERVATION = "observation"
"""Journal event: what one turn saw, and the settings it saw it under.

Written by every executed turn, whether or not anything happened, because an agent reads the run as a **time series** and its own memory does not survive a session boundary — there are several of those in a night. If the series is not written down it does not exist, and the 6am agent inherits a list of open items with no idea which are getting worse (workflow.md, *The journal records observations, not only decisions*).

It is also what makes degrading `_steward.md` possible: the settings in force are recorded here, so a turn that cannot parse the file has somewhere to read the last good ones from.
"""

ACTION = "action"
"""Journal event: something Steward did, and how it turned out."""


class TendError(Exception):
    """A turn could not be run at all.

    Distinct from a turn that ran and found problems, which is a `TendResult` with things in it. This is the machinery failing rather than the run.
    """


@dataclass(frozen=True)
class Refused:
    """A tend that did not run, because somebody else holds the claim.

    A value rather than an error, and the caller should treat it as an ordinary outcome: a timer firing while an agent is mid-tend is the common case, not the edge, and the right response is to do nothing at all. The work is already being done.
    """

    held: Held


@dataclass(frozen=True)
class TendResult:
    """What a turn saw, and what it did about it."""

    summary: Summary
    """Where the run stands."""

    queued: list[SpawnWorker]
    """Would have been spawned, but the pool is full."""

    drift: bool
    """The definition file no longer hashes to what the committed manifest recorded.

    Reported and never acted on. Capture is expensive and a definition is a file a human edits live, so a turn that re-read it automatically would eventually read a half-saved edit — and applying a delta means weighing whether it looks like a mistake, which is judgement (workflow.md, *One trigger, and one gate on it*).
    """

    degraded: str | None
    """Why `_steward.md` could not be read, when a turn ran on the last known good settings anyway."""

    claim: Held | None
    """Who holds the run claim. Only ever set by `status`, which reports one rather than taking one."""

    broke: Held | None
    """The wedged holder this turn killed to get the claim, if there was one."""

    spawned: list[str] = field(default_factory=list[str])
    reaped: list[str] = field(default_factory=list[str])
    archived: list[str] = field(default_factory=list[str])

    failures: list[str] = field(default_factory=list[str])
    """Actions that could not be carried out. One failing action does not fail the turn — the rest still run, and the next turn decides again from what it finds."""

    executed: bool = False
    """Whether the actions were carried out (`tend`) or discarded (`status`)."""

    progress: Progress = field(default_factory=Progress)
    """One row per task: samples done, in flight, and queued, the budget the leading sample is closest to spending, and the headline metric.

    Where `summary` counts what Steward is doing, this is what the *run* is doing — the question anybody opening a status view actually has. Sample counts and the live columns come from different places (the log header and the worker's own socket), which is why it is assembled here rather than inside `reconcile`.
    """


def tend(
    workspace: Workspace,
    *,
    max_workers: int | None = None,
    max_samples: int | None = None,
    break_stale: bool = True,
) -> TendResult | Refused:
    """Run one turn of the supervision loop.

    Args:
        workspace: The workspace to tend.
        max_workers: Worker ceiling for this turn, overriding `_steward.md`.
        max_samples: Sample concurrency for this turn, overriding the definition.
        break_stale: Kill a wedged claim holder and take the claim from it.

    Returns:
        What the turn saw and did, or a `Refused` naming the holder that would not give up the claim.

    Raises:
        TendError: The turn could not be run — no committed manifest, an unreadable log directory, or a `_steward.md` that cannot be parsed and no history to fall back on.
        ManifestError: The committed manifest is not a manifest.
        ManifestVersionError: The manifest was captured by a different `task_identifier` version, so nothing in the log directory can be matched to it.
    """
    manifest = _manifest(workspace)

    outcome = acquire(workspace.claim, command="tend", break_stale=break_stale)
    if isinstance(outcome, Held):
        return Refused(held=outcome)

    with outcome as claim:
        # inside the claim, because resolving these can *write* — a degraded
        # `_steward.md` says so in `steward.log` — and a refused turn has to be
        # a genuine no-op. A timer firing every ten minutes against an agent's
        # long-held claim would otherwise leave a line each time it fired
        settings = _settings(
            workspace, max_workers=max_workers, max_samples=max_samples, execute=True
        )
        if claim.broke is not None:
            # machinery rather than a fact about the eval set, so it goes to
            # the operational log -- but it must be *somewhere*, because a
            # deterministic wedge is a kill loop and the only evidence of one
            # is a line per turn beside a `status.md` that never advances
            steward_log(
                workspace.log,
                f"broke a wedged claim held by pid {claim.broke.pid} "
                f"({claim.broke.command or 'unknown command'}, held since "
                f"{claim.broke.since or 'an unrecorded time'})",
            )
        return _turn(
            workspace,
            manifest,
            settings,
            execute=True,
            claim=None,
            broke=claim.broke,
        )


def status(
    workspace: Workspace,
    *,
    max_workers: int | None = None,
    max_samples: int | None = None,
) -> TendResult:
    """Report where the run stands, and what the next turn would do.

    `tend --dry-run`: the same reads and the same decision, with the actions discarded. That makes it a **preview** rather than a state dump — "6 tasks: 3 complete, 2 running, 1 errored; the next tend would launch 2 workers" is what both a human and an agent actually want to see before authorizing an interval.

    Not the cheap one, though. It performs the same reads; only the side effects are withheld.

    Args:
        workspace: The workspace to report on.
        max_workers: Worker ceiling to preview against.
        max_samples: Sample concurrency to preview against.

    Returns:
        What a turn would see and do.

    Raises:
        TendError: The state could not be read.
        ManifestError: The committed manifest is not a manifest.
        ManifestVersionError: The manifest cannot be matched against this inspect_ai.
    """
    settings = _settings(
        workspace, max_workers=max_workers, max_samples=max_samples, execute=False
    )
    manifest = _manifest(workspace)
    return _turn(
        workspace,
        manifest,
        settings,
        execute=False,
        claim=read_claim(workspace.claim),
        broke=None,
    )


@dataclass(frozen=True)
class _Settings:
    """What this turn is operating under, and whether that is the file's own answer."""

    pool: Pool
    degraded: str | None


def _turn(
    workspace: Workspace,
    manifest: Manifest,
    settings: _Settings,
    *,
    execute: bool,
    claim: Held | None,
    broke: Held | None,
) -> TendResult:
    """Both dispositions, differing only in whether the actions are carried out."""
    drift = _drifted(workspace, manifest)
    log_dir = _log_dir(workspace, manifest)

    inflight = resolve_inflight(workspace.inflight, workspace.workers)
    # read even for a `status`, which is the disposition that most needs it: a
    # person types it to satisfy their curiosity, and a settled directory of two
    # thousand logs is two thousand header reads it can skip entirely
    cache = read_attempt_cache(workspace.observed)
    try:
        logs = observe_logs(log_dir, cache=cache)
    except OSError as ex:
        # scheduling into a directory that cannot be read would multiply the
        # loss; running workers are left alone, since one that still holds its
        # own handle may finish normally (execution.md, *When the substrate fails*)
        if execute:
            steward_log(
                workspace.log, f"could not read the log directory {log_dir}: {ex}"
            )
        raise TendError(
            f"the log directory {log_dir} could not be read, so nothing can be "
            f"scheduled against it: {ex}"
        ) from ex

    observed = observe_tasks(manifest, logs)
    decision = reconcile(manifest, inflight, observed, pool=settings.pool)
    progress = Progress(rows=task_progress(observed, _live(inflight)))

    result = TendResult(
        summary=decision.summary,
        queued=decision.queued,
        drift=drift,
        degraded=settings.degraded,
        claim=claim,
        broke=broke,
        executed=execute,
        progress=progress,
    )
    if not execute:
        return result

    acted = _act(workspace, manifest, log_dir, decision.actions)
    result = TendResult(
        summary=decision.summary,
        queued=decision.queued,
        drift=drift,
        degraded=settings.degraded,
        claim=claim,
        broke=broke,
        spawned=acted.spawned,
        reaped=acted.reaped,
        archived=acted.archived,
        failures=acted.failures,
        executed=True,
        progress=progress,
    )

    # narrowed to what this listing named *minus what this turn just moved out
    # of it*, which is what keeps it bounded. The subtraction is because the
    # listing was taken before the archiving; without it every archived log
    # would linger one extra turn. An archive that failed simply costs a header
    # read next turn, which is why this does not consult `acted`.
    #
    # **A tend writes this and a status does not.** The cache is disposable and
    # a write of it mutates nothing about the run, so the rule is not really
    # about damage -- it is that *writes nothing* is a promise worth being able
    # to make without a footnote, and the tend on the timer keeps the cache warm
    # for whoever types `status` between turns anyway.
    moved = {
        action.location for action in decision.actions if isinstance(action, ArchiveLog)
    }
    write_attempt_cache(workspace.observed, cache.keep({*logs.locations} - moved))

    _record(workspace, result, pool=settings.pool)
    _write_status(workspace, result)
    return result


@dataclass
class _Acted:
    """What actually happened, which is not always what was decided."""

    spawned: list[str] = field(default_factory=list[str])
    reaped: list[str] = field(default_factory=list[str])
    archived: list[str] = field(default_factory=list[str])
    failures: list[str] = field(default_factory=list[str])


def _act(
    workspace: Workspace,
    manifest: Manifest,
    log_dir: str,
    actions: list[Action],
) -> _Acted:
    """Carry out a turn's actions, in the order `reconcile` put them in.

    **One failing action does not fail the turn.** A spawn that cannot start a process, or a log that cannot be moved, is recorded and stepped over — the remaining actions are independent of one another, and the next turn decides again from what it finds rather than from what was attempted. Aborting instead would let one bad task hold up an entire fleet, every ten minutes, forever.
    """
    acted = _Acted()
    spawns: list[SpawnWorker] = []

    for action in actions:
        if isinstance(action, SpawnWorker):
            # held back so the fleet is built once and only if there is
            # something to spawn. `reconcile` already orders spawns last, so
            # collecting them here preserves that order rather than imposing it
            spawns.append(action)
        else:
            _carry_out(workspace, log_dir, action, acted)

    if spawns:
        _spawn_all(workspace, manifest, log_dir, spawns, acted)
    return acted


def _carry_out(
    workspace: Workspace,
    log_dir: str,
    action: ReapWorker | ArchiveLog,
    acted: _Acted,
) -> None:
    """Do one thing that is not a spawn, and survive it not working."""
    try:
        match action:
            case ReapWorker():
                record_exited(workspace.inflight, worker=action.worker.worker)
                acted.reaped.append(action.worker.worker)

            case ArchiveLog():
                # journalled after the move, and carrying where the log
                # actually went: an entry describing an archive that never
                # happened is a lie in the one record nothing can rebuild,
                # where a missing entry is recoverable by looking
                destination = archive_log(action.location, log_dir)
                append_event(
                    workspace.journal,
                    ACTION,
                    action="archive",
                    reason="orphaned",
                    identifier=action.identifier,
                    location=action.location,
                    archived=destination,
                )
                acted.archived.append(destination)
    except Exception as ex:
        _failed(workspace, acted, _describe(action), ex)


def _spawn_all(
    workspace: Workspace,
    manifest: Manifest,
    log_dir: str,
    spawns: list[SpawnWorker],
    acted: _Acted,
) -> None:
    """Spawn every worker this turn decided on.

    The fleet is built once, and a failure to build one is reported **once** rather than per spawn: it resolves the definition and checks the packages its type needs, so when it fails it fails identically for every task, and forty copies of one message is not forty pieces of information.
    """
    try:
        fleet = _fleet(workspace, manifest, log_dir)
    except Exception as ex:
        _failed(workspace, acted, "could not prepare to spawn workers", ex)
        return

    for action in spawns:
        try:
            acted.spawned.append(fleet.spawn(action).worker)
        except Exception as ex:
            _failed(workspace, acted, _describe(action), ex)


def _failed(workspace: Workspace, acted: _Acted, what: str, ex: Exception) -> None:
    """Record an action that did not work, in both places it belongs.

    The exception's type is named as well as its message, because a bare message from an unexpected failure is often unattributable — and this path deliberately catches everything, so an ordinary bug can land here too.
    """
    failure = f"{what}: {type(ex).__name__}: {ex}"
    acted.failures.append(failure)
    steward_log(workspace.log, failure)


def _describe(action: Action) -> str:
    match action:
        case ReapWorker():
            return f"could not reap {action.worker.worker}"
        case ArchiveLog():
            return f"could not archive {action.location}"
        case SpawnWorker():
            return f"could not spawn {action.key}"


def _fleet(workspace: Workspace, manifest: Manifest, log_dir: str) -> Fleet:
    """What every worker this turn spawns will share."""
    definition = _definition(workspace, manifest)
    if not definition.exists():
        raise TendError(
            f"the definition {definition} no longer exists, so no worker can run "
            f"it — restore it, or run `steward launch` against its replacement"
        )
    return Fleet(
        definition=definition,
        type=manifest.source.type,
        log_dir=log_dir,
        eval_set_id=resolve_eval_set_id(log_dir, manifest.eval_set_id),
        workers_dir=workspace.workers,
        inflight=workspace.inflight,
        # the workspace rather than wherever a timer happened to fire from, so
        # a definition's relative paths resolve the same way every turn
        cwd=workspace.root,
        args=manifest.source.args or None,
    )


def _manifest(workspace: Workspace) -> Manifest:
    """Desired state, or an explanation of why there is none."""
    try:
        return read_manifest(workspace.manifest)
    except FileNotFoundError as ex:
        raise TendError(
            "this workspace has no committed manifest, so there is nothing to "
            "converge toward — run `steward launch` to capture the definition "
            "and commit it as desired state"
        ) from ex
    except OSError as ex:
        raise TendError(f"{workspace.manifest} could not be read: {ex}") from ex


def _settings(
    workspace: Workspace,
    *,
    max_workers: int | None,
    max_samples: int | None,
    execute: bool,
) -> _Settings:
    """What to operate under, degrading to the last known good where it must.

    A human may edit `_steward.md` at 10pm with a fleet up, and a typo in it must not stop the fleet converging — that is exactly the unattended failure the timer exists to prevent. So a file that will not parse falls back to the settings the last turn recorded, and says so loudly enough that nobody mistakes the run for one following the file.

    **Falling back needs somewhere to fall back to.** With no `observation` in the journal there is no last known good, and running on Steward's own defaults would silently discard whatever the operator wrote — the one outcome worse than stopping. So the first turn after a bad edit refuses, and every turn after a good one degrades.
    """
    try:
        directives = read_directives(workspace.directives)
    except DirectivesError as ex:
        if (last := _last_pool(workspace)) is None:
            raise
        pool = Pool(
            max_workers=max_workers if max_workers is not None else last.max_workers,
            max_samples=max_samples if max_samples is not None else last.max_samples,
            stall_after=last.stall_after,
        )
        if execute:
            steward_log(
                workspace.log,
                f"{workspace.directives.name} could not be read ({ex}); "
                f"running on the settings the last turn recorded",
            )
        return _Settings(pool=pool, degraded=str(ex))

    return _Settings(
        pool=resolve_pool(directives, max_workers=max_workers, max_samples=max_samples),
        degraded=None,
    )


def _last_pool(workspace: Workspace) -> Pool | None:
    """The settings the most recent turn ran under, from the journal."""
    try:
        events = read_journal(workspace.journal).events
    except OSError:
        return None

    for event in reversed(events):
        if event.type != OBSERVATION:
            continue
        recorded = event.payload.get("settings")
        if not isinstance(recorded, dict):
            continue
        settings = cast(dict[str, Any], recorded)
        if (workers := _positive(settings.get("max_workers"))) is None:
            continue
        return Pool(
            max_workers=workers,
            max_samples=_positive(settings.get("max_samples")),
            stall_after=_positive(settings.get("stall_after")) or 1,
        )
    return None


def _positive(value: Any) -> int | None:
    """A positive integer from a journal payload, which may hold anything."""
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _record(workspace: Workspace, result: TendResult, *, pool: Pool) -> None:
    """Append this turn's observation to the journal.

    After the actions rather than before, so it records what happened rather than what was intended. A turn interrupted between the two loses its observation and repeats no work: the next turn re-reads the same directory and reaches the same place.
    """
    summary = result.summary
    append_event(
        workspace.journal,
        OBSERVATION,
        tasks=summary.tasks,
        states=summary.states,
        reasons=summary.reasons,
        running=summary.running,
        spawned=len(result.spawned),
        reaped=len(result.reaped),
        archived=len(result.archived),
        queued=summary.queued,
        stalled=summary.stalled,
        orphans_running=summary.orphans_running,
        unreadable=summary.unreadable,
        drift=result.drift,
        degraded=result.degraded,
        failures=result.failures,
        # what this turn ran under, which is what a later turn reads back when
        # `_steward.md` will not parse
        settings={
            "max_workers": pool.max_workers,
            "max_samples": pool.max_samples,
            "stall_after": pool.stall_after,
        },
    )


def _write_status(workspace: Workspace, result: TendResult) -> None:
    """Rewrite `status.md`, atomically, and never at the cost of the turn.

    Written through a temporary file and renamed, because this is the file a remote reader watches and half of it would read as a run in a state it was never in. A failure to write it is machinery: the turn already happened, and the journal already recorded it.
    """
    body = status_markdown(result)
    temporary = workspace.status.with_name(f".{workspace.status.name}.tmp")
    try:
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(workspace.status)
    except OSError as ex:
        # `missing_ok` covers the temporary never having been created; it does
        # not cover the unlink itself failing, and a cleanup that raises out of
        # a handler would fail a turn that already happened
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        steward_log(workspace.log, f"could not write {workspace.status.name}: {ex}")


def _live(inflight: InFlight) -> LiveFleet:
    """Ask the running workers how they are getting on.

    **Only the ones that are running, and only when some are.** The in-flight record already answers *is anything alive* for free, so a finished campaign — the common shape late on — pays nothing at all for the live columns. A worker that has not yet bound its control socket has no entry here either; it is in the window before its `eval_set()` boundary, where there is genuinely nothing to ask.
    """
    targets = [
        LiveTarget(identifier=worker.identifier, pid=worker.pid, socket=worker.socket)
        for worker in inflight.running
        if worker.socket is not None
    ]
    return read_fleet(targets)


def _definition(workspace: Workspace, manifest: Manifest) -> Path:
    """Where the manifest's definition is, anchored to the workspace when relative."""
    path = Path(manifest.source.path)
    return path if path.is_absolute() else workspace.root / path


def _drifted(workspace: Workspace, manifest: Manifest) -> bool:
    """Whether the definition has changed since it was captured.

    One hash of one file, cheap enough for every turn, and the guard against the failure that actually costs a night: an edit made at 11pm that nobody applied, converging all night toward the manifest captured before it. Never acted on here — `launch` is the only verb that reads a definition.
    """
    try:
        return definition_hash(_definition(workspace, manifest)) != (
            manifest.source.content_hash
        )
    except OSError:
        # gone, or unreadable: either way it is not the file that was captured,
        # which is the same thing drift means
        return True


def _log_dir(workspace: Workspace, manifest: Manifest) -> str:
    """The run's log directory: the definition's own, or the workspace's by default."""
    configured = manifest.options.get("log_dir")
    if isinstance(configured, str) and configured:
        if "://" in configured or Path(configured).is_absolute():
            return configured
        # a relative log_dir is relative to where the definition was captured,
        # which for a workspace's own definition is the workspace
        return str(workspace.root / configured)
    return str(workspace.logs)
