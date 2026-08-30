"""`launch` — the one verb that reads the definition, and therefore the only one that decides.

Everything else in Steward converges toward a manifest somebody else committed. This is where the manifest comes from, which makes it a composition rather than a component: capture, gate, commit, restore, stop, arm, record, tend. Nothing here is new machinery; what is new is the **order**, and four of the orderings have a wrong-looking alternative worth stating.

**The claim is taken before the capture, and held across all of it.** Capture is minutes for a Hawk config, which is a long time to hold a lock whose whole design rests on being short-lived (`_workspace.claim`). The alternative is worse in a way that costs results rather than time: two launches interleaving one's capture with the other's commit, or — the real hazard — a scheduled tend firing mid-launch and spawning workers for tasks the commit is about to orphan. A tend that fires meanwhile is refused, which is the ordinary path rather than a problem: *a tend is built to be interrupted*, and the next interval converges.

**The gate sits between the capture and the commit, and there is nowhere else it could sit.** `reconcile` archives orphans unconditionally, by design — once desired state says a task is not in the eval set, moving its logs is bookkeeping. So the instant a manifest that orphans tasks is written, the 02:00 tend archives them with nobody present. Consent is taken here or it is not taken.

**Arming happens before the first tend**, so a launch whose own turn fails still leaves the run supervised and the next interval picks it up. The timer firing during the launch's own tend is a non-event — it meets the held claim and does nothing.

**The `launched` event is appended before the tend**, so that turn's item projection already sees it. Otherwise a `--no-timer` launch's own first `status.md` would report the run as supervised, which is the one thing execution.md §8.3 asks this event to prevent.

**The restore happens after the commit**, because what a restore is *for* is the identifiers the new manifest asks for. Before the commit there is nothing authoritative to restore against.

**Two correctness traps are written into the code below rather than left to a reader.** The definition path is recorded in the manifest verbatim and resolved by every later tend against the workspace root, so a path relative to the shell's cwd would send every subsequent turn looking somewhere else. And capture's cwd must equal the fleet's, because the manifest records no cwd — a launch typed from a subdirectory would enumerate under one and execute under another, and nothing downstream could detect it.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .._evalset.archive import archive_dir, restore_log
from .._evalset.detect import DefinitionType
from .._evalset.manifest import (
    Manifest,
    ManifestError,
    read_manifest,
    write_manifest,
)
from .._evalset.observe import ObservedLogs, observe_logs
from .._evalset.read import ReadEvalSetError, read_eval_set
from .._schedule import RunningWorker
from .._tend import TendResult, tend
from .._timer import (
    Armament,
    TimerError,
    arm,
    disarm,
    explain_env,
    unavailable_credentials,
)
from .._util.duration import DurationError
from .._worker import Stop, StopRequest, resolve_inflight, stop_workers
from .._workspace import (
    LAUNCHED,
    Claim,
    Held,
    Workspace,
    acquire,
    append_event,
    ensure_gitignore,
    read_directives,
    resolve_interval,
    steward_log,
)
from .delta import Change, Delta, compute_delta

STORE_ENV = "INSPECT_STEWARD_STORE"
"""Where the log store is, for a machine rather than for a project.

A store is frequently shared — several machines pointing at one S3 prefix, so a colleague's completed task is one your next launch does not have to run — which is why the location is environmental and `--store` is a per-launch override of it (execution.md §5.6).
"""


class LaunchError(Exception):
    """A launch could not be completed.

    A message for a person, never a traceback: everything reachable here is a condition of the machine or the definition rather than a defect. A refusal at the archive gate is *not* one of these — that is an outcome with a delta attached, and the delta is the whole point of it.
    """


@dataclass(frozen=True)
class Launch:
    """What a launch did, or what it stopped short of doing."""

    manifest: Manifest
    """The manifest just captured, committed or not."""

    delta: Delta
    """What committing it would change."""

    committed: bool = False
    """Whether the manifest became desired state. `False` only ever means the archive gate refused — every other failure raises, because every other failure leaves nothing to report."""

    armed: Armament | None = None
    """The timer installed, or `None` where the launch was told not to arm one."""

    disarmed: str | None = None
    """The scheduler a `--no-timer` launch *removed*, or `None` where there was nothing to remove. Skipping the arming is not the same act as ending the supervision, and on a re-launch only the second one is what was asked for."""

    restored: list[str] = field(default_factory=list[str])
    """Logs moved back out of the archive, by their new location."""

    stopped: list[Stop] = field(default_factory=list[Stop])
    """Workers stopped because the manifest no longer names what they were running."""

    turn: TendResult | None = None
    """The first turn, which is what actually spawns the first workers."""

    failures: list[str] = field(default_factory=list[str])
    """Things that did not work but did not stop the launch — a log that would not move, a worker that would not stop. Reported here and in `steward.log`, and left for the next turn, which decides again from what it finds."""


def launch(
    workspace: Workspace,
    definition: Path,
    *,
    args: dict[str, Any] | None = None,
    type: DefinitionType | None = None,
    accept_archive: bool = False,
    timer: bool = True,
    env_check: bool = True,
    store: str | None = None,
    max_workers: int | None = None,
    max_tasks: int | None = None,
    max_samples: int | None = None,
    break_stale: bool = True,
) -> Launch | Held:
    """Capture a definition, commit it as desired state, arm a timer, and tend once.

    Args:
        workspace: The workspace to launch in.
        definition: The definition to capture. Recorded relative to the workspace root where it lives inside one, so that a workspace which moves still resolves it.
        args: Arguments for the definition. `None` reuses the committed manifest's, which is what keeps a re-launch from capturing a different eval set and proposing to archive the first one. **An empty mapping is not the same thing**: it asks for the definition's own defaults, which is the only way back once a launch has passed arguments.
        type: Explicit definition type. `None` reuses the committed manifest's, then falls back to detection.
        accept_archive: Commit even though tasks would leave `logs/`.
        timer: Arm a timer. `False` launches unsupervised and records that it did.
        env_check: Refuse to arm when a scheduled tend would not inherit this shell's credentials. Checked **before** the capture, so a Hawk config does not spend five minutes resolving packages on the way to a refusal.
        store: Log store for this run — a path, `auto`, or `none` — overriding `INSPECT_STEWARD_STORE`. **Recorded and otherwise inert**: nothing reads a store until publication exists (step 33), and the only validation available before then is that the value is not empty.
        max_workers: Worker processes for the first turn, overriding `_steward.md`. `None` expresses no preference and defers to the file, which itself defaults to a process per task — it does not request that width.
        max_tasks: Tasks in flight at once for the first turn, overriding the definition's `max_tasks`. `None` defers to it.
        max_samples: Sample concurrency for the first turn.
        break_stale: Kill a wedged claim holder and take the claim from it.

    Returns:
        What the launch did, or a `Held` naming the holder that would not give up the claim. A `Launch` with `committed` false is the archive gate refusing, which is an answer rather than a failure.

    Raises:
        LaunchError: The definition could not be read, the credentials check refused, no timer could be armed, or the committed manifest could not be replaced.
        DirectivesError: `_steward.md` could not be parsed. Launching against settings nobody can read is the one place degrading would be wrong — a tend degrades because a fleet must keep converging, and a launch has nothing to keep going.
        ManifestError: What is committed is not a manifest. A launch is exactly the command that replaces it, so this reports rather than overwrites: the delta would otherwise be computed against nothing and propose archiving a directory full of real results.
    """
    interval = _interval(workspace)
    resolved = _store(store)

    # before the capture and before the claim, because both of the things below
    # are cheap and one of them is a refusal. A five-minute Hawk capture that
    # ends in *put your API key in .env* is a worse version of the same message
    #
    # **This is the one write that precedes the archive gate**, and it is not a
    # write about the run: no manifest, no journal entry, no log moved. It has
    # to be here because the refusal on the next line names `.env` as the
    # remedy, and advice that leaks credentials into a commit is worse than no
    # advice (execution.md §8.3). Recorded at the moment it happens rather than
    # after the launch returns, so that a launch which then refuses — or raises
    # — still leaves the change accounted for
    if ignored := ensure_gitignore(workspace):
        steward_log(workspace.log, f"added to .gitignore: {', '.join(ignored)}")

    if timer and env_check:
        missing = unavailable_credentials(workspace.env, os.environ)
        if missing:
            raise LaunchError(explain_env(missing, workspace.env))

    outcome = acquire(workspace.claim, command="launch", break_stale=break_stale)
    if isinstance(outcome, Held):
        return outcome

    with outcome as claim:
        return _launch(
            workspace,
            definition,
            claim,
            args=args,
            type=type,
            accept_archive=accept_archive,
            timer=timer,
            interval=interval,
            store=resolved,
            max_workers=max_workers,
            max_tasks=max_tasks,
            max_samples=max_samples,
        )


def _launch(
    workspace: Workspace,
    definition: Path,
    claim: Claim,
    *,
    args: dict[str, Any] | None,
    type: DefinitionType | None,
    accept_archive: bool,
    timer: bool,
    interval: int,
    store: str | None,
    max_workers: int | None,
    max_tasks: int | None,
    max_samples: int | None,
) -> Launch:
    """The launch itself, with the claim in hand for the whole of it."""
    committed = _committed(workspace)
    manifest = _capture(
        workspace,
        definition,
        args=args if args is not None else _prior_args(committed),
        type=type
        if type is not None
        else (committed.source.type if committed else None),
    )
    _refuse_scanners(manifest)

    log_dir = _log_dir(workspace, manifest)
    # the *committed* manifest's directory, which is where the run's results
    # actually are. Ordinarily the same string and read once; when a `log_dir`
    # edit has moved it, the difference is the whole of what the delta would
    # otherwise miss
    previous = _log_dir(workspace, committed) if committed is not None else log_dir

    inflight = resolve_inflight(workspace.inflight, workspace.workers)
    logs = _observe(log_dir)
    delta = compute_delta(
        manifest,
        committed,
        logs=logs,
        archived=_observe(archive_dir(log_dir)),
        running=inflight.running,
        stranded=_observe(previous) if previous != log_dir else None,
    )

    if not (delta.additive or accept_archive):
        # the refusal is the whole product of this call: the caller prints the
        # delta and the operator decides. Nothing has been written
        return Launch(manifest=manifest, delta=delta)

    try:
        write_manifest(manifest, workspace.manifest)
    except OSError as ex:
        raise LaunchError(
            f"the manifest could not be committed, so nothing has changed: {ex}"
        ) from ex

    failures: list[str] = []
    restored = _restore(workspace, delta, log_dir, failures)
    stopped = _stop(workspace, delta, inflight.running, logs, failures)

    armed, disarmed, refused = _supervise(workspace, interval, timer=timer)

    # after the arming so it can name the scheduler, and before the tend so
    # that turn's items already know this run was launched
    append_event(
        workspace.journal,
        LAUNCHED,
        definition=manifest.source.path,
        tasks=len(manifest.tasks),
        timer=armed.scheduler if armed is not None else None,
        store=store,
    )
    if refused is not None:
        raise LaunchError(refused)

    turn = tend(
        workspace,
        max_workers=max_workers,
        max_tasks=max_tasks,
        max_samples=max_samples,
        claim=claim,
    )
    return Launch(
        manifest=manifest,
        delta=delta,
        committed=True,
        armed=armed,
        disarmed=disarmed,
        restored=restored,
        stopped=stopped,
        # `tend` cannot refuse a claim it was handed, so the union is only
        # formally present here
        turn=turn if isinstance(turn, TendResult) else None,
        failures=failures,
    )


def _capture(
    workspace: Workspace,
    definition: Path,
    *,
    args: dict[str, Any] | None,
    type: DefinitionType | None,
) -> Manifest:
    """Execute the definition and read the eval set out of it.

    **Three paths matter, and conflating any two of them breaks something different.**

    `cwd` is the workspace root, because that is where `_fleet` pins every worker and the manifest records no cwd of its own — enumerating under one directory and executing under another is undetectable downstream, since nothing in the manifest says which was used.

    The path *read* is absolute. `read_eval_set` resolves what it is given against the **process's** cwd — it checks the file exists and hashes it before any subprocess with a `cwd` of its own is involved — so a relative path here means `steward launch` typed from a subdirectory of its own workspace cannot find its own definition.

    The path *recorded* is relative to the workspace root wherever the definition lives inside one, because `_tend.turn` resolves a relative one against the root. Absolute would work and would break the first time somebody moves or copies the workspace; relative to the shell's cwd would break immediately.

    So the manifest's `source.path` is rewritten after the capture rather than steered by the argument. `read_eval_set` keeps the simpler contract — *the path you hand me is the path I read and the path I record* — and the one caller with a reason to want them different is the one that states the reason.
    """
    absolute = definition.resolve()
    try:
        recorded = absolute.relative_to(workspace.root)
    except ValueError:
        # outside the workspace, which is legitimate — a shared definition read
        # by several workspaces — and can only be named absolutely
        recorded = absolute

    try:
        manifest = read_eval_set(absolute, args=args, type=type, cwd=workspace.root)
    except (ValueError, ReadEvalSetError) as ex:
        raise LaunchError(str(ex)) from ex

    return manifest.model_copy(
        update={"source": manifest.source.model_copy(update={"path": str(recorded)})}
    )


def _refuse_scanners(manifest: Manifest) -> None:
    """Stop a definition that scans, before any of its workers would.

    Selection mode rejects scanners outright: one scan directory is shared by a whole eval set and its bookkeeping assumes a single writer. Capture reports whether the definition declares one precisely so a runner learns it here rather than from every worker failing identically at its `eval_set()` boundary (configuration.md, *Flow's store*).
    """
    if manifest.options.get("scanners") is True:
        raise LaunchError(
            "this definition declares a scanner, which cannot run under "
            "Steward: one scan directory is shared by a whole eval set and its "
            "bookkeeping assumes a single writer, so concurrent workers would "
            "race in it. Remove the scanner from the definition and scan the "
            "log directory afterwards instead"
        )


def _committed(workspace: Workspace) -> Manifest | None:
    """Desired state as it stands, or `None` where this workspace has never launched.

    **A manifest that will not read raises rather than reading as absent.** Absent means every task is an addition and the delta needs no acceptance; unreadable means the same thing arrived at by accident, and would propose archiving a directory full of results nobody agreed to give up.
    """
    try:
        return read_manifest(workspace.manifest)
    except FileNotFoundError:
        return None
    except ManifestError:
        raise
    except OSError as ex:
        raise LaunchError(
            f"{workspace.manifest} could not be read, so what this run is "
            f"currently converging toward is unknown: {ex}"
        ) from ex


def _prior_args(committed: Manifest | None) -> dict[str, Any] | None:
    """The arguments the committed manifest was captured with, if any.

    What keeps a re-launch honest. `steward launch` typed a second time without the `-A` flags of the first would capture a *different* eval set and propose archiving everything the run has done — a data-losing outcome produced by forgetting a flag, which is precisely the shape of mistake the archive gate exists to catch and precisely the shape it should not have to.

    **Only reached when the caller said nothing about arguments.** A default with no way out is a trap, and this one had one: with `None` meaning *reuse* and any `-A` meaning *set*, a run that had once been launched with arguments could never be launched without them again. `--no-args` passes an empty mapping, which never arrives here.
    """
    if committed is None or not committed.source.args:
        return None
    return dict(committed.source.args)


def _observe(log_dir: str) -> ObservedLogs:
    """Read a directory of logs, or say why it could not be read.

    Uncached, unlike a turn's read of the same directory. A launch happens once and has just spent minutes in a subprocess; the cache exists for the turn that runs every ten minutes, and warming it here would mean deciding what to do about the archive's entries, which no turn ever reads.
    """
    try:
        return observe_logs(log_dir)
    except OSError as ex:
        raise LaunchError(
            f"{log_dir} could not be read, so what this run has already done is "
            f"unknown and nothing can be committed against it: {ex}"
        ) from ex


def _restore(
    workspace: Workspace, delta: Delta, log_dir: str, failures: list[str]
) -> list[str]:
    """Move every restorable log back into the run's directory.

    A log that will not move costs a re-run of one task and nothing else, so it is recorded and stepped over — the same rule `_tend.turn._act` follows, for the same reason: one stuck file must not stop the rest of a launch.
    """
    restored: list[str] = []
    for row in delta.of(Change.RESTORE):
        for location in row.logs:
            try:
                restored.append(restore_log(location, log_dir))
            except OSError as ex:
                _failed(workspace, failures, f"could not restore {location}", ex)
    return restored


def _stop(
    workspace: Workspace,
    delta: Delta,
    running: list[RunningWorker],
    logs: ObservedLogs,
    failures: list[str],
) -> list[Stop]:
    """Stop the work the commit just made pointless.

    Two reasons, and `Delta.stopping` has already unioned them. **Its task left the manifest**: `reconcile` deliberately leaves an orphan's logs alone while something is still running it, so a worker left up here writes into `logs/` for hours against a definition nothing agrees with, and its logs are archived only once it stops of its own accord. **Or the log directory left underneath it**: a worker's destination is fixed in its selection document at spawn, so after a relocation the whole fleet is writing where nothing will look, and each task runs again from nothing once its worker exits.

    **What is stopped is tasks, not necessarily processes.** The two coincide at the default width and come apart once a run is packed: a worker holding one archived task and four live ones must lose the one and keep the four. Relocation is the exception and needs no subset — the directory moved out from under everything in the process.
    """
    wanted = set(delta.stopping)
    wholesale = delta.wholesale
    leaving = delta.leaving
    targets = [
        StopRequest(
            worker=worker,
            identifiers=(
                worker.identifiers
                if worker.worker in wholesale
                else tuple(
                    identifier
                    for identifier in worker.identifiers
                    if identifier in leaving
                )
            ),
        )
        for worker in running
        if worker.worker in wanted
    ]
    stopped = stop_workers(targets, locations=_locations(logs))

    for stop in stopped:
        if not stop.graceful:
            failures.append(
                f"worker {stop.worker} was {stop.outcome.value}: {stop.detail}"
            )
            steward_log(workspace.log, failures[-1])
    return stopped


def _locations(logs: ObservedLogs) -> dict[str, str]:
    """Log location to task identifier, which is how a control-channel row is named.

    A running task's log is created when it starts, so the observation this launch already read names every task the fleet is working on. The pre-boundary window is the exception, and a worker in it has no row to match either.
    """
    return {
        attempt.location: identifier
        for identifier, attempts in logs.attempts.items()
        for attempt in attempts
    }


def _supervise(
    workspace: Workspace, interval: int, *, timer: bool
) -> tuple[Armament | None, str | None, str | None]:
    """Put the run's supervision where the operator asked for it.

    **`--no-timer` disarms rather than merely skipping the arming**, and the difference only shows up on a re-launch. Skipping leaves the previous launch's scheduler entry firing every interval, `read_armed` still reporting it, and the operator told the opposite of what is true — while the run goes on scheduling expensive work against an explicit instruction not to. execution.md §8.3 says `--no-timer` *launches unsupervised*, which is a statement about the state the command leaves behind rather than about one step it omits.

    **Failing to disarm fails the launch, exactly as failing to arm does.** The asymmetry would be tempting — an arm that fails leaves a run nothing will tend, a disarm that fails leaves one something *will* — but both end with the command having said something untrue about who is watching, and between the two the unwanted timer is the one that spends money.

    Reported rather than raised, so that the `launched` event is written either way: a run whose manifest is committed *was* launched, and `status` has to be able to say it is unsupervised. The caller raises immediately afterwards.

    Args:
        workspace: The workspace.
        interval: Seconds between tends, for an arming.
        timer: Whether the run is to be supervised.

    Returns:
        What was armed, what was disarmed, and why neither happened.
    """
    if timer:
        try:
            return arm(workspace, interval), None, None
        except TimerError as ex:
            return (
                None,
                None,
                (
                    f"the manifest is committed but no timer could be armed, so "
                    f"nothing would tend this run: {ex}\nfix the above and run "
                    f"`steward timer arm`, or relaunch with --no-timer to drive it "
                    f"by hand"
                ),
            )
    try:
        return None, disarm(workspace), None
    except TimerError as ex:
        return (
            None,
            None,
            (
                f"the manifest is committed but the timer already armed here could "
                f"not be removed, so this run is still being tended on a schedule "
                f"despite --no-timer: {ex}\nfix the above and run `steward timer "
                f"disarm`"
            ),
        )


def _interval(workspace: Workspace) -> int:
    """How often this workspace asks to be tended.

    From `_steward.md` or Steward's default, and deliberately not from a flag: an interval is a standing property of a workspace rather than a property of one launch, which is the argument that put it in that file in the first place (plan.md §9). Somebody who wants a different one for a single run arms it themselves.
    """
    try:
        return resolve_interval(read_directives(workspace.directives))
    except DurationError as ex:
        raise LaunchError(str(ex)) from ex


def _store(store: str | None) -> str | None:
    """The store this launch names, checked as far as anything can check it yet.

    `auto`, `none`, or a path (execution.md §5.6). The location is a property of the machine rather than of the project, so it lives in `INSPECT_STEWARD_STORE` and `--store` is a per-launch override of it — which means there has to be something to override, and a launch on a machine with a store configured has to record *that* store rather than nothing.

    **An exported-but-empty variable is unset; an empty value typed on the command line is a typo.** The same reading `_timer.env` gives a credential, and for the same reason: refusing to launch because somebody's shell profile exports an empty variable would be refusing a correct setup, while `--store ''` is a deliberate act that did not say anything.

    **Recorded and inert.** Reads and publication arrive with step 33, and until then the only thing that can be wrong with a value is that it says nothing — a path is not resolved, because a store is created when something first publishes to it rather than when a launch mentions it.
    """
    if store is None:
        return os.environ.get(STORE_ENV) or None
    if not store.strip():
        raise LaunchError(
            "--store takes a path, `auto`, or `none`, and was given an empty value"
        )
    return store


def _log_dir(workspace: Workspace, manifest: Manifest) -> str:
    """The run's log directory: the definition's own, or the workspace's by default.

    The same resolution `_tend.turn` performs, and it has to be: the delta is computed against the directory the fleet will write into, and two answers to *where do the logs go* would make a launch propose archiving a directory no tend ever reads.
    """
    configured = manifest.options.get("log_dir")
    if isinstance(configured, str) and configured:
        if "://" in configured or Path(configured).is_absolute():
            return configured
        return str(workspace.root / configured)
    return str(workspace.logs)


def _failed(
    workspace: Workspace, failures: list[str], what: str, ex: Exception
) -> None:
    """Record something that did not work, in both places it belongs."""
    failure = f"{what}: {type(ex).__name__}: {ex}"
    failures.append(failure)
    steward_log(workspace.log, failure)


__all__ = ["STORE_ENV", "Launch", "LaunchError", "launch"]
