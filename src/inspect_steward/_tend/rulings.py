"""Applying rulings: the tend acts on what a person (or a standing policy) decided.

Step 23 made an errored class a question with a recorded answer; this module is the acting half. A `rerun` ruling on a **running** task is applied warm — `inspect ctl sample requeue`, one errored sample at a time, re-running in-attempt under a fresh uuid — and on a **landed** one by invalidation: the log is reopened (`invalidate_samples` + `write_eval_log`), which flips the observation to `INVALIDATED` and authorizes the respawn reconcile schedules first. A task-kind rerun needs no executor at all — the errored task respawns mechanically, and the ruling's whole application is the stall-guard forgiveness (`reconcile`'s `ruled`). `limit:` samples in a still-running task are the one population neither path can reach (requeue refuses non-errored samples; the landed path waits) — they wait for the task to complete, deliberately.

**The memory is per-target, derived, and keyed by the ruling's instant.** Every application journals an `action` event (`ruling_applied`, folded by `_anomaly.applied`) recording exactly which targets it acted on; whether a ruling stands fully applied is recomputed each turn as `unapplied(census) == []`, never stored — a stored flag written on a turn where some targets 409'd would be a false witness the pass check then trusts. A re-ruling (a new ts) owes fresh applications by construction, which is what makes re-applying after a failed re-run automatic.

**Effect first, journal after** — the `_carry_out` archive argument: an entry describing an effect that never happened is a lie in the one record nothing can rebuild, where a crash between the two costs a repeat against upstream operations that are idempotent (invalidation skips already-invalid samples; a repeated requeue answers `changed: false` and books as converged).

Standing pre-authorizations (`preauthorized:` in `_steward.yaml`) become ordinary `ruling` events here, `by: policy`, before the applier reads the fold — so a pattern's ruling lands and applies in one turn, and everything downstream (precedent, forgiveness, the pass check) treats it exactly like a person's.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Protocol

from inspect_ai.log import (
    ProvenanceData,
    invalidate_samples,
    read_eval_log,
    write_eval_log,
)

from .._anomaly.applied import RULING_APPLIED, Applied
from .._anomaly.fold import Pending, covered_refs, unapplied
from .._anomaly.model import (
    ABSORBING,
    Anomalies,
    Anomaly,
    AnomalyState,
    Disposition,
    Ruling,
    composed_effect,
    honest,
)
from .._evalset.instances import Instance, InstanceBatch
from .._evalset.observe import ObservedTasks, TaskObservation
from .._schedule import InFlight
from .._worker import LiveFleet, Unavailable, requeue_sample
from .._workspace import ACTION, RULING, Workspace, append_event, steward_log


class Acted(Protocol):
    """The slice of the turn's `_Acted` this executor writes to.

    A protocol rather than an import, so the runtime graph stays one-way — the turn imports this module, and this module needs only the shared posture: failures cost their class and never the turn, and a journal append flips the flag that makes the turn re-read its own record.
    """

    failures: list[str]
    journalled: bool


def rerun_ruled(anomalies: Anomalies) -> dict[str, str]:
    """The tasks a standing `rerun` ruling covers, each with the ruling's instant.

    What the turn hands `reconcile`: the stall guard forgives attempt history at or before the instant, and the scheduler sorts these tasks first. The newest ruling wins where windows overlap, because forgiveness runs from the latest authorization.
    """
    ruled: dict[str, str] = {}
    for anomaly in anomalies.open:
        ruling = anomaly.ruling
        if (
            anomaly.state is not AnomalyState.RULED
            or ruling is None
            or ruling.disposition is not Disposition.RERUN
        ):
            continue
        for identifier in anomaly.evidence.tasks:
            if identifier not in ruled or ruling.ts > ruled[identifier]:
                ruled[identifier] = ruling.ts
    return ruled


def policy_rulings(
    anomalies: Anomalies, preauthorized: Mapping[str, str] | None
) -> tuple[list[Pending], list[str]]:
    """The rulings standing pre-authorizations grant this turn, and the grants declined.

    One pending `ruling` per absorbing window whose class matches a pattern — first match wins, in file order — worded `by: policy` with the pattern named in the reason and the effect composed exactly as `steward rule` would compose it. Once per generation by construction: the ruling closes the window, and a recurrence opens the next generation to be matched afresh, precedent accumulating.

    Three grants are declined with a note rather than recorded. A disposition the class's kind cannot honestly carry (the `honest` matrix — a pattern must not grant what a person could not type). A `rerun` of a substrate-flagged class (a standing pattern is not the human look §9.1 requires before re-running into broken machinery). And a `rerun` of a class whose earlier re-run already failed — after a `reran_failed` a person must look, or policy re-runs every fresh generation forever.

    Returns:
        The pending ruling events, and one note per declined grant for `steward.log`.
    """
    if not preauthorized:
        return [], []
    rulings: list[Pending] = []
    notes: list[str] = []
    pending_rerun = {
        anomaly.class_key
        for anomaly in anomalies.open
        if anomaly.state is AnomalyState.RULED
    }
    for anomaly in anomalies.open:
        if anomaly.state not in ABSORBING:
            continue
        key = anomaly.class_key
        matched = next(
            (
                (pattern, action)
                for pattern, action in preauthorized.items()
                if fnmatchcase(key, pattern)
            ),
            None,
        )
        if matched is None:
            continue
        pattern, action = matched
        decided = Disposition(action)
        if not honest(anomaly.kind, decided):
            notes.append(
                f"preauthorized pattern '{pattern}' grants {decided.value}, "
                f"which cannot mark {key} (a {anomaly.kind} class) — skipped"
            )
            continue
        if decided is Disposition.RERUN:
            if anomaly.substrate:
                notes.append(
                    f"preauthorized pattern '{pattern}' grants rerun, and {key} "
                    f"looks like the machinery under the run — a person must "
                    f"look before it re-runs (skipped)"
                )
                continue
            if key in pending_rerun:
                # an authorized re-run's outcome is still pending; a second
                # standing grant on the same class waits for it
                continue
            if any(window.failed_resolutions > 0 for window in anomalies.of_class(key)):
                notes.append(
                    f"preauthorized pattern '{pattern}' grants rerun, and a "
                    f"re-run of {key} already failed — a person must rule "
                    f"(skipped)"
                )
                continue
        rulings.append(
            Pending(
                type=RULING,
                fields={
                    "class": key,
                    "disposition": decided.value,
                    "reason": f"preauthorized in _steward.yaml ('{pattern}')",
                    "by": "policy",
                    "effect": composed_effect(anomalies, key, decided),
                },
            )
        )
    return rulings, notes


def apply_rulings(
    workspace: Workspace,
    anomalies: Anomalies,
    batches: Sequence[InstanceBatch],
    applied: Applied,
    inflight: InFlight,
    fleet: LiveFleet,
    observed: ObservedTasks,
    spawned: set[str],
    acted: Acted,
) -> None:
    """Carry out every standing `rerun` ruling's unapplied remainder.

    Per decision — RULED sample-kind windows grouped by `(class, ruling instant)`, since a class-scoped ruling can close two generations at once and they must not double-apply: the ruled population is `unapplied` over the windows' merged refs — the one membership definition the routing and the pass check share — grouped per evidence task and partitioned by liveness. A running task's targets are warm-requeued; a landed one's are invalidated in its current attempt; a task with nothing left and no witness yet gets a `converged` record (a human requeued by hand, or upstream retries absorbed the errors — without the witness the window would stick RULED forever). What could not be reached this turn — a 409 mid-finish, a busy worker, a task a worker was just spawned for — is deferred with **no record**, so exactly the remainder retries next turn.

    One failing class costs that class, never the turn — the `_act` posture.
    """
    census = {batch.class_key: batch for batch in batches}
    lookup = {task.identifier: task for task in observed.tasks}
    unreadable = {entry.location for entry in observed.unreadable}
    running = inflight.running_identifiers
    # a class-scoped ruling closes every non-terminal window of its class, so
    # two generations can stand RULED under the same instant -- they are one
    # decision and get one application: refs and evidence tasks merged, one
    # `_apply`, one journal event, never a duplicate requeue for a shared task
    grouped: dict[tuple[str, str], tuple[Anomaly, Ruling, set[str], list[str]]] = {}
    for anomaly in anomalies.open:
        ruling = anomaly.ruling
        if (
            anomaly.state is not AnomalyState.RULED
            or ruling is None
            or ruling.disposition is not Disposition.RERUN
            or anomaly.kind == "task"
        ):
            continue
        entry = grouped.get((anomaly.class_key, ruling.ts))
        if entry is None:
            grouped[(anomaly.class_key, ruling.ts)] = (
                anomaly,
                ruling,
                set(covered_refs(anomaly, ruling.ts)),
                list(anomaly.evidence.tasks),
            )
        else:
            entry[2].update(covered_refs(anomaly, ruling.ts))
            entry[3].extend(
                task for task in anomaly.evidence.tasks if task not in entry[3]
            )
    for anomaly, ruling, refs, tasks in grouped.values():
        try:
            _apply(
                workspace,
                anomaly,
                ruling,
                tasks,
                frozenset(refs),
                census.get(anomaly.class_key),
                applied,
                running,
                fleet,
                lookup,
                unreadable,
                spawned,
                acted,
            )
        except Exception as ex:
            failure = (
                f"could not apply the rerun ruling on {anomaly.class_key}: "
                f"{type(ex).__name__}: {ex}"
            )
            acted.failures.append(failure)
            steward_log(workspace.log, failure)


def _apply(
    workspace: Workspace,
    anomaly: Anomaly,
    ruling: Ruling,
    tasks: list[str],
    refs: frozenset[str],
    batch: InstanceBatch | None,
    applied: Applied,
    running: set[str],
    fleet: LiveFleet,
    lookup: dict[str, TaskObservation],
    unreadable: set[str],
    spawned: set[str],
    acted: Acted,
) -> None:
    """One class's application: act, then journal what actually landed."""
    key = anomaly.class_key
    remaining = unapplied(
        list(batch.instances) if batch is not None else [],
        key,
        ruling.ts,
        applied,
        refs,
    )
    by_task: dict[str, list[Instance]] = {}
    for instance in remaining:
        by_task.setdefault(instance.task, []).append(instance)

    requeued: list[dict[str, Any]] = []
    invalidated: list[dict[str, Any]] = []
    converged: list[str] = []
    deferred: list[dict[str, Any]] = []
    for identifier in tasks:
        targets = by_task.get(identifier, [])
        if not targets:
            # nothing left to act on. If nothing was ever recorded either, the
            # convergence itself is the news: without this witness the pass
            # check could never credit a warm recovery. But only an
            # *authoritative* empty census can say so -- when one of this
            # task's logs would not read, absence means blindness, and a
            # converged record here would be a false witness the pass check
            # trusts forever
            if applied.witness(key, ruling.ts, identifier) is None:
                if _blinded(lookup.get(identifier), unreadable):
                    steward_log(
                        workspace.log,
                        f"holding the converged record for {key} on {identifier}: "
                        f"a log would not read, and an empty census is not "
                        f"evidence while the read fails",
                    )
                else:
                    converged.append(identifier)
            continue
        if identifier in running:
            _warm(fleet, identifier, targets, requeued, deferred)
        elif identifier in spawned:
            # a worker for this task was spawned moments ago and may be opening
            # the log to resume it -- rewriting it now is the one race the
            # executor can create, and waiting a turn costs nothing
            deferred.extend(
                _deferral(target, "a worker was just spawned for it")
                for target in targets
            )
        else:
            _landed(
                workspace,
                lookup.get(identifier),
                anomaly,
                ruling,
                targets,
                invalidated,
                deferred,
            )

    if requeued or invalidated or converged:
        fields: dict[str, Any] = {
            "action": RULING_APPLIED,
            "class": key,
            "for": ruling.ts,
            "by": ruling.by,
        }
        if requeued:
            fields["requeued"] = requeued
        if invalidated:
            fields["invalidated"] = invalidated
        if converged:
            fields["converged"] = converged
        if deferred:
            # provenance only, never memory: deferral is recomputed next turn
            # as census-minus-applied
            fields["deferred"] = deferred
        append_event(workspace.journal, ACTION, **fields)
        acted.journalled = True
    elif deferred:
        reasons = "; ".join(
            f"{entry['id']}:{entry['epoch']} in {entry['task']} ({entry['why']})"
            for entry in deferred
        )
        steward_log(
            workspace.log,
            f"deferred applying the rerun ruling on {key}: {reasons}",
        )


def _warm(
    fleet: LiveFleet,
    identifier: str,
    targets: list[Instance],
    requeued: list[dict[str, Any]],
    deferred: list[dict[str, Any]],
) -> None:
    """Requeue a running task's ruled samples, one directive per target.

    `changed: true` and `changed: false` both book as applied — a no-op means the re-run is already coming, and deferring it would re-requeue the re-run's own later failure, which is an unruled re-run. Only an unreachable worker or a 409 defers, with no record, so the remainder retries next turn (landed by then, ordinarily, where the invalidation path collects it).
    """
    live = fleet.tasks.get(identifier)
    if live is None or live.unavailable is not None or not live.task_id:
        why = (
            f"the worker is not answering ({live.unavailable})"
            if live is not None and live.unavailable is not None
            else "the worker has not reported a task id"
        )
        deferred.extend(_deferral(target, why) for target in targets)
        return
    for target in targets:
        outcome = requeue_sample(live.task_id, target.sample_id, target.epoch)
        if isinstance(outcome, Unavailable):
            deferred.append(_deferral(target, f"{outcome.kind}: {outcome.detail}"))
        else:
            requeued.append(
                {"task": target.task, "id": target.sample_id, "epoch": target.epoch}
            )


def _landed(
    workspace: Workspace,
    observation: TaskObservation | None,
    anomaly: Anomaly,
    ruling: Ruling,
    targets: list[Instance],
    invalidated: list[dict[str, Any]],
    deferred: list[dict[str, Any]],
) -> None:
    """Invalidate a landed task's ruled samples, in its current attempt only.

    Instances sitting in a superseded attempt are booked as needing nothing — a later attempt already answered those samples, and re-opening a superseded log would re-run work the current one holds. Targets in the current attempt are invalidated by uuid (a `score:` class, whose instance is the whole log, invalidates every sample). Whether anything is already invalid is read **per sample**, never off the log's `invalidated` header — that flag says *some* sample was invalidated, and trusting it would book one class's ruling as applied on the strength of another's write. Every target already carrying `invalidation` is the crash-recovery record — the effect landed on an earlier turn whose journal append never did — and is booked without a write.
    """
    identifier = targets[0].task
    current = observation.current if observation is not None else None
    if current is None:
        deferred.extend(
            _deferral(target, "no current attempt to reopen") for target in targets
        )
        return
    superseded = [target for target in targets if target.location != current.location]
    if superseded:
        invalidated.append(
            {
                "task": identifier,
                "location": "",
                "uuids": [target.uuid for target in superseded if target.uuid],
                "note": "superseded — a later attempt already answered these samples",
            }
        )
    active = [target for target in targets if target.location == current.location]
    if not active:
        return
    uuids = [target.uuid for target in active if target.uuid]
    if not uuids and anomaly.kind != "score":
        # an error or limit instance with no recorded uuid cannot be targeted,
        # and "all" would re-run samples nobody ruled on
        deferred.extend(
            _deferral(target, "no sample uuid recorded") for target in active
        )
        return
    try:
        log = read_eval_log(current.location)
        already = {
            sample.uuid
            for sample in log.samples or []
            if sample.invalidation is not None
        }
        needed = (
            [uuid for uuid in uuids if uuid not in already]
            if uuids
            else (
                []
                if all(s.invalidation is not None for s in log.samples or [])
                else ["all"]
            )
        )
        if not needed:
            invalidated.append(
                {
                    "task": identifier,
                    "location": current.location,
                    "eval_id": current.eval_id,
                    "uuids": uuids,
                    "note": "found already invalidated",
                }
            )
            return
        write_eval_log(
            invalidate_samples(
                log,
                needed if uuids else "all",
                ProvenanceData(
                    author=ruling.by or "steward", reason=ruling.reason or None
                ),
            )
        )
    except ValueError as ex:
        # unknown uuids: the log moved under the census -- next turn's re-read
        # supplies the fresh membership
        message = f"the log refused the uuids ({ex})"
        steward_log(
            workspace.log,
            f"could not invalidate {current.location} for {anomaly.class_key}: "
            f"{message}",
        )
        deferred.extend(_deferral(target, message) for target in active)
        return
    invalidated.append(
        {
            "task": identifier,
            "location": current.location,
            "eval_id": current.eval_id,
            "uuids": uuids,
        }
    )


def _blinded(observation: TaskObservation | None, unreadable: set[str]) -> bool:
    """Whether any of this task's logs failed to read this turn — its census is then unknown, never authoritative."""
    if observation is None or not unreadable:
        return False
    return any(
        attempt.location in unreadable
        for attempt in (observation.current, *observation.superseded)
        if attempt is not None
    )


def _deferral(target: Instance, why: str) -> dict[str, Any]:
    return {
        "task": target.task,
        "id": target.sample_id,
        "epoch": target.epoch,
        "why": why,
    }


@dataclass(frozen=True)
class Dispositions:
    """Per task, what each errored sample's class has been ruled — the reporting fold.

    Pure and shared: the markdown table's errored-cell split and `--json` read it now, and step 26's signoff will refuse while anything is `undecided`.
    """

    by_task: dict[str, dict[str, int]] = field(
        default_factory=dict[str, dict[str, int]]
    )
    """Task identifier to bucket counts, over `error:` instances — `rerunning`, `excluded`, `zeroed`, `scored`, `accepted`, `undecided`."""

    excluded: int = 0
    """Samples ruled excluded, run-wide, over error and limit instances — the "Scores are over n of m" numerator's complement."""

    zeroed: int = 0
    """Samples ruled zeroed, run-wide."""


BUCKETS = {
    Disposition.RERUN: "rerunning",
    Disposition.EXCLUDE: "excluded",
    Disposition.ZERO: "zeroed",
    Disposition.SCORE: "scored",
    Disposition.ACCEPT: "accepted",
    # a dismissal accepted the data as it stands, with no mark to carry
    Disposition.DISMISS: "accepted",
}


def dispositions(
    batches: Sequence[InstanceBatch],
    anomalies: Anomalies,
    current: Mapping[str, str],
) -> Dispositions:
    """Fold the census against the rulings in force.

    Only instances in their task's **current** attempt are counted, so the split agrees with the errored cell beside it: the cell counts the current log's samples, and a superseded attempt's instances — still in the census for the routing's sake — are not part of the results being described.

    Args:
        batches: Detection's full census.
        anomalies: The fold, post-policy — the rulings in force.
        current: Task identifier to its current attempt's log location.

    Returns:
        Bucket counts per task over `error:` instances, plus the run-wide exclusion and zero totals over error and limit instances.
    """
    by_task: dict[str, dict[str, int]] = {}
    excluded = zeroed = 0
    for batch in batches:
        if batch.kind not in ("error", "limit"):
            continue
        # per instance, the window that covers it decides -- an excluded
        # first generation must not read as undecided because a fresh second
        # one opened beside it. Coverage is `covered_refs` under the ruling in
        # force, so a re-run failure a *later* ruling answered takes that
        # ruling's bucket, while one still awaiting a fresh decision -- or an
        # instance no window has absorbed at all -- reads undecided: nothing
        # ruled has covered it
        by_ref: dict[str, str] = {}
        for window in anomalies.of_class(batch.class_key):
            covered = (
                covered_refs(window, window.ruling.ts)
                if window.ruling is not None
                else window.refs
            )
            for ref in covered:
                by_ref[ref] = _window_bucket(window)
        for instance in batch.instances:
            if instance.location != current.get(instance.task):
                continue
            bucket = by_ref.get(instance.ref, "undecided")
            if batch.kind == "error":
                counts = by_task.setdefault(instance.task, {})
                counts[bucket] = counts.get(bucket, 0) + 1
            if bucket == "excluded":
                excluded += 1
            elif bucket == "zeroed":
                zeroed += 1
    return Dispositions(by_task=by_task, excluded=excluded, zeroed=zeroed)


def _window_bucket(window: Anomaly) -> str:
    """The disposition in force over one window's instances, as a report bucket."""
    if window.state is AnomalyState.RULED:
        return "rerunning"
    if window.open or window.ruling is None:
        return "undecided"
    return BUCKETS.get(window.ruling.disposition, "undecided")


__all__ = [
    "BUCKETS",
    "Dispositions",
    "apply_rulings",
    "dispositions",
    "policy_rulings",
    "rerun_ruled",
]
