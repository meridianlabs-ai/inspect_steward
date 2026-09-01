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
    MetadataEdit,
    ProvenanceData,
    edit_eval_log,
    invalidate_samples,
    read_eval_log,
    write_eval_log,
)

from .._anomaly.applied import RULING_APPLIED, Applied
from .._anomaly.fold import Pending, covered_refs, unapplied
from .._anomaly.model import (
    ABSORBING,
    SAMPLE_MARKED,
    Anomalies,
    Anomaly,
    AnomalyState,
    Disposition,
    Ruling,
    composed_effect,
    honest,
)
from .._evalset.classify import task_error_class
from .._evalset.instances import Instance, InstanceBatch, in_results
from .._evalset.observe import ObservedTasks, TaskObservation
from .._schedule import InFlight
from .._worker import LiveFleet, Unavailable, requeue_sample
from .._workspace import ACTION, RULING, Workspace, append_event, steward_log

ACCEPTANCE_KEY = "steward_accepted"
"""The log-metadata key one acceptance writes. A fixed name rather than one composed from the class, because the reader who wants it months later is grepping for a word, and a key assembled per class would be unfindable."""

FLIPPABLE = frozenset({"error", "cancelled"})
"""The log statuses an acceptance may amend. `started` is deliberately absent — see `_flip`."""


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


def accepted_tasks(anomalies: Anomalies) -> dict[str, str]:
    """The tasks a standing `accept` ruling has settled, each with the instant that settled it.

    The counterpart to `rerun_ruled`, in the same shape and for the same reason: a rerun ruling authorizes more attempts, an acceptance ends them, and both have to be told apart from a later decision about something else. `reconcile` neither spawns nor stalls a task named here.

    **`task:` kinds only, and the narrowness is the whole safety property.** A `limit:` or `score:` acceptance is a claim about the data *inside* a log — the operator kills stand, the zero headline stands — and says nothing about whether the task should run again; its task is ordinarily `COMPLETE` anyway. Letting one latch would mean that accepting the operator kills in a task which is *also* short for an unrelated reason silently ended that task, which is a decision nobody made. A `task:` class is the only kind whose subject is the attempt itself.

    **The instant is carried rather than flattened away**, which a set lost. The latch releases on a launch that postdates the accepting ruling — and with only a set of identifiers, `_latched` had to ask whether *any* settled window postdated the launch, so an unrelated `exclude` or `dismiss` recorded on the same task after a relaunch re-latched it and quietly stopped its respawns. Each identifier is now compared against the decision that actually accepted it. The newest wins where two acceptances cover one task, because the latch runs from the latest one.

    The same predicate the executor amends a log on (`_flip`), and the two agree on purpose. The executor once covered every accepted kind, guarded by the log's own status — which held only while *a `limit:` acceptance's log has already succeeded* held, and that is an assumption about a run rather than a property of one.
    """
    accepted: dict[str, str] = {}
    for anomaly in anomalies.settled:
        ruling = anomaly.ruling
        if (
            anomaly.state is not AnomalyState.ACCEPTED
            or anomaly.kind != "task"
            or ruling is None
            or ruling.disposition is not Disposition.ACCEPT
        ):
            continue
        for identifier in anomaly.evidence.tasks:
            if identifier not in accepted or ruling.ts > accepted[identifier]:
                accepted[identifier] = ruling.ts
    return accepted


def affected_refs(
    batches: Sequence[InstanceBatch],
    current: Mapping[str, str],
    reused: Mapping[str, frozenset[str]] = {},
) -> dict[str, frozenset[str]]:
    """Per class, the instances it left **in the data** — those in a current attempt, by ref.

    The narrowing every report-facing count needs, applied through the one predicate that owns it (`_evalset.instances.in_results`) because three readers here need the same answer: the errored cell's split, the effect sentence a ruling composes, and `anomalies.md`'s scope. A window's own `evidence.count` is what it *absorbed*, and a sample that failed, was re-run and failed again is two instances of one row.

    **Refs rather than a count**, because one of the three readers needs to narrow further. A class key outlives its generations, so the class's current population can span a settled generation and an open one — and the effect sentence a new ruling composes is about the windows it is ruling, not about everything the key has ever covered. A count cannot be intersected; the refs can.

    Pure, and cheap: the census holds anomalous instances rather than samples, so this is a pass over the failures rather than over the run.

    Args:
        batches: Detection's census.
        current: Task identifier to its current attempt's log location.
        reused: Per resumed task, the sample uuids its current log holds — what keeps a scan finding on a reused sample from silently leaving the results.

    Returns:
        Class key to the refs of its instances that are in the results.
    """
    affected: dict[str, set[str]] = {}
    for batch in batches:
        for instance in batch.instances:
            if in_results(instance, current, reused):
                affected.setdefault(batch.class_key, set()).add(instance.ref)
    return {key: frozenset(refs) for key, refs in affected.items()}


def policy_rulings(
    anomalies: Anomalies,
    preauthorized: Mapping[str, str] | None,
    affected: Mapping[str, frozenset[str]] | None = None,
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
                    "effect": composed_effect(anomalies, key, decided, affected),
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
    """Carry out every standing ruling's unapplied remainder — re-runs, and acceptances.

    Per decision — windows grouped by `(class, ruling instant)`, since a class-scoped ruling can close two generations at once and they must not double-apply.

    A **rerun** takes RULED sample-kind windows: the ruled population is `unapplied` over the windows' merged refs — the one membership definition the routing and the pass check share — grouped per evidence task and partitioned by liveness. A running task's targets are warm-requeued; a landed one's are invalidated in its current attempt; a task with nothing left and no witness yet gets a `converged` record (a human requeued by hand, or upstream retries absorbed the errors — without the witness the window would stick RULED forever). What could not be reached this turn — a 409 mid-finish, a busy worker, a task a worker was just spawned for — is deferred with **no record**, so exactly the remainder retries next turn.

    An **acceptance** takes ACCEPTED windows and reads from `anomalies.settled` rather than `.open`, because that is where the fold puts them: every accepting disposition settles its window on the spot (`_ruled_state`), so a carrier looking in `.open` would find nothing at all. **The two differ in the grain of their target, and everything else follows from that.** A rerun acts on samples, so its remainder is `unapplied` over the census; an acceptance acts on *logs*, so its remainder is the window's evidence tasks minus the ones this ruling already reached. Same doctrine one level up: nothing stores *fully applied*, a deferral leaves no record, and one failing class costs that class and never the turn — the `_act` posture.
    """
    census = {batch.class_key: batch for batch in batches}
    lookup = {task.identifier: task for task in observed.tasks}
    unreadable = {entry.location for entry in observed.unreadable}
    running = inflight.running_identifiers
    for anomaly, ruling, refs, tasks in _grouped(
        anomalies.open, Disposition.RERUN
    ).values():
        if anomaly.kind == "task":
            # a task-kind rerun needs no executor: the errored task respawns
            # mechanically, and the ruling's whole application is reconcile's
            # stall-guard forgiveness
            continue
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

    for anomaly, ruling, _covered, tasks in _grouped(
        anomalies.settled, Disposition.ACCEPT
    ).values():
        try:
            _accept(
                workspace,
                anomaly,
                ruling,
                tasks,
                applied,
                running,
                lookup,
                spawned,
                acted,
            )
        except Exception as ex:
            failure = (
                f"could not apply the acceptance on {anomaly.class_key}: "
                f"{type(ex).__name__}: {ex}"
            )
            acted.failures.append(failure)
            steward_log(workspace.log, failure)


def _grouped(
    windows: Sequence[Anomaly],
    disposition: Disposition,
) -> dict[tuple[str, str], tuple[Anomaly, Ruling, set[str], list[str]]]:
    """One decision per `(class, ruling instant)`, with its refs and evidence tasks merged.

    A class-scoped ruling closes every non-terminal window of its class, so two generations can stand under the same instant — they are one decision and get one application: refs and evidence tasks merged, one journal event, never a duplicate act for a shared task. Two generations under two *different* rulings are two decisions and stay apart.
    """
    grouped: dict[tuple[str, str], tuple[Anomaly, Ruling, set[str], list[str]]] = {}
    for anomaly in windows:
        ruling = anomaly.ruling
        if ruling is None or ruling.disposition is not disposition:
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
    return grouped


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


def _accept(
    workspace: Workspace,
    anomaly: Anomaly,
    ruling: Ruling,
    tasks: list[str],
    applied: Applied,
    running: set[str],
    lookup: dict[str, TaskObservation],
    spawned: set[str],
    acted: Acted,
) -> None:
    """Carry out one acceptance: mark the logs it covers `success`, and record that it landed.

    An acceptance says *this attempt is the result, with a caveat the report carries*. Steward's own machinery already honours that through the latch (`accepted_tasks`), but the log on disk still says `error`, and every downstream reader — `eval_set`, the viewer, `samples_df`, whoever opens the directory in six months — reads the wreckage rather than the decision. So the header is amended to agree with the person who ruled, and the ruling travels inside the log as provenance.

    **Two signals say a task has already been accepted, and they answer different questions.** The journal record (`Applied.accepted_tasks`) is the *memory*: the only thing that can settle a partial application, a deferral or a re-ruling, and the only witness for a `limit:` or `score:` acceptance whose log was already `success` and needed no write at all. The log's own status is the *guard*: a crash between the effect and the journal append leaves the amendment landed and the record missing, and the next turn must find `success` and book it rather than swap a header a second time. This is `_landed`'s "found already invalidated" reasoning applied to a header field instead of a sample field.
    """
    key = anomaly.class_key
    remaining = [
        identifier
        for identifier in tasks
        if identifier not in applied.accepted_tasks(key, ruling.ts)
    ]
    accepted: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for identifier in remaining:
        if identifier in running:
            # a header swap rewrites a zip's central directory in place, and
            # doing that to a file a live worker holds open is the one way this
            # executor could destroy a result rather than fail to change one.
            # The latch has already stopped the respawns, so the wait is
            # bounded by the worker's own exit
            deferred.append(_task_deferral(identifier, "a worker is running it"))
            continue
        if identifier in spawned:
            deferred.append(
                _task_deferral(identifier, "a worker was just spawned for it")
            )
            continue
        accepted.append(_flip(lookup.get(identifier), identifier, anomaly, ruling))

    if accepted:
        fields: dict[str, Any] = {
            "action": RULING_APPLIED,
            "class": key,
            "for": ruling.ts,
            "by": ruling.by,
            "accepted": accepted,
        }
        if deferred:
            # provenance only, never memory: the remainder is recomputed next
            # turn as evidence-tasks-minus-applied
            fields["deferred"] = deferred
        append_event(workspace.journal, ACTION, **fields)
        acted.journalled = True
    elif deferred:
        reasons = "; ".join(f"{entry['task']} ({entry['why']})" for entry in deferred)
        steward_log(
            workspace.log, f"deferred applying the acceptance on {key}: {reasons}"
        )


def _flip(
    observation: TaskObservation | None,
    identifier: str,
    anomaly: Anomaly,
    ruling: Ruling,
) -> dict[str, Any]:
    """Amend one landed log to say what was decided about it, where there is one to amend.

    Most outcomes write nothing and still book the task, because *the acceptance landed here* is true whether or not a byte moved: a class whose logs already succeeded (most `limit:` and `score:` acceptances) needs no mark, and re-examining it every turn for the life of the run is the cost of not recording it.

    **Only an acceptance of the log's own failure amends the log**, which is the same predicate the latch uses and no longer the asymmetry the design claimed. The old rule — every accepted kind, guarded by the log's status — rested on *a `limit:` or `score:` acceptance's log has already succeeded*, which is an assumption rather than a guarantee. A log holding operator-killed samples can also terminate with a scorer error, and accepting `limit:operator` then cleared a task failure nobody had ruled on: the halting error went into the acceptance metadata under a decision that was about something else, the header said `success`, and the task went on respawning because a `limit:` acceptance deliberately does not latch. So the gate is the class's own kind, and then the class itself — a task that now fails differently from the failure somebody accepted is the same mistake one level down.

    **A log still being written is accepted without being rewritten.** Upstream's in-place amendment drops `header.json` from the zip and writes a fresh one — and it will happily *create* one in a log whose header is still `_journal/start.json`, which is not editing a finished eval but manufacturing one, with `results` still `None`. The file would then claim `success` and still read as having no results to the very observation that wrote it. `task:vanished` is exactly this case, and the latch is what settles it.

    **Only the current attempt is ever touched.** Which log is current decides which numbers the run reports, so amending a superseded one would change the answer rather than record a decision about it.
    """
    current = observation.current if observation is not None else None
    if current is None:
        return {"task": identifier, "flipped": False, "note": "no log to accept"}

    if anomaly.kind != "task":
        return {
            "task": identifier,
            "location": current.location,
            "flipped": False,
            "note": "the log's own failure is not what was accepted",
        }

    # header only on both halves, and the write's flag is the one that matters:
    # a header-only read followed by a full write re-inits the recorder and
    # persists `samples or []` -- which, over this read, is nothing at all
    log = read_eval_log(current.location, header_only=True)
    if log.status == "success":
        return {
            "task": identifier,
            "location": current.location,
            "flipped": False,
            "note": "already success",
        }
    if log.status not in FLIPPABLE:
        return {
            "task": identifier,
            "location": current.location,
            "flipped": False,
            "note": "the log is still being written",
        }
    if log.error is not None:
        # **only where there is a halting error to erase.** A cancelled log
        # carries none, so there is nothing a mismatched class could cost; an
        # errored one carries the account of what stopped it, and clearing that
        # under a ruling about a different failure is this guard's whole point
        failure = task_error_class(log.error.message, log.error.traceback)
        if failure != anomaly.class_key:
            return {
                "task": identifier,
                "location": current.location,
                "flipped": False,
                "note": f"the log now fails as {failure}, not {anomaly.class_key}",
            }

    record: dict[str, Any] = {
        "class": anomaly.class_key,
        "generation": anomaly.generation,
        "ruling": ruling.ts,
        "by": ruling.by,
        "reason": ruling.reason,
        "effect": ruling.effect,
        "status_before": log.status,
    }
    if log.error is not None:
        # the halting error moves rather than being kept or dropped: left
        # standing under `success` it puts an error payload on a success row in
        # every listing and the viewer, a contradiction a reader cannot
        # resolve -- and deleting it would lose the only account of what
        # happened from the one file that outlives the workspace
        record["error_before"] = {
            "message": log.error.message,
            "traceback": log.error.traceback,
        }
    edited = edit_eval_log(
        log,
        [MetadataEdit(metadata_set={ACCEPTANCE_KEY: record})],
        ProvenanceData(
            author=ruling.by or "steward",
            reason=ruling.reason or None,
            metadata={"class": anomaly.class_key, "ruling": ruling.ts},
        ),
    )
    edited = edited.model_copy(update={"status": "success", "error": None})
    # the ETag is populated by a read from S3 and is a documented no-op
    # elsewhere, so one call site is a compare-and-swap remotely and
    # best-effort locally -- where the in-place swap is not atomic, accepted
    # because it touches only a landed log no worker holds and happens at most
    # once per (class, ruling, task) for the life of the run
    write_eval_log(
        edited, current.location, if_match_etag=edited.etag, header_only=True
    )
    return {
        "task": identifier,
        "location": current.location,
        "eval_id": current.eval_id,
        "from": record["status_before"],
        "flipped": True,
    }


def _task_deferral(identifier: str, why: str) -> dict[str, Any]:
    return {"task": identifier, "why": why}


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

    Pure and shared: the markdown table's errored-cell split, `--json`, and the signoff gate all read it — the gate refusing while anything is `undecided`.
    """

    by_task: dict[str, dict[str, int]] = field(
        default_factory=dict[str, dict[str, int]]
    )
    """Task identifier to bucket counts, over `error:` instances — `rerunning`, `excluded`, `zeroed`, `scored`, `accepted`, `undecided`."""

    excluded: int = 0
    """Samples ruled excluded, run-wide, over error and limit instances — the "Scores are over n of m" numerator's complement."""

    zeroed: int = 0
    """Samples ruled zeroed, run-wide."""

    affected: dict[str, frozenset[str]] = field(
        default_factory=dict[str, frozenset[str]]
    )
    """Per class, the instances it left **in the data** — those in a current attempt, which is what a ruling's effect sentence is counting.

    **The number a window cannot supply.** `Evidence.count` is what the window absorbed, and a sample that failed, was re-run and failed again is two instances of one row: counting it composes *6 samples excluded from scoring* over three excluded samples, three lines above a denominator line that says three. Computed here because this is the one fold that already holds both the census and the current-attempt map, and read by `composed_effect` so that a person's ruling and a policy's compose the same sentence from the same number.
    """


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
    reused: Mapping[str, frozenset[str]] = {},
) -> Dispositions:
    """Fold the census against the rulings in force.

    Only instances **in the results** are counted (`_evalset.instances.in_results`), so the split agrees with the errored cell beside it: the cell counts the current log's samples, and a superseded attempt's instances — still in the census for the routing's sake — are not part of the results being described.

    **The run-wide totals are per sample row, never per instance**, which is what stops one row being counted twice. A batch is a class, and one sample can be in two of them — a sample that errored and was flagged by a scanner, or one two scanners flagged — so summing over instances would report *4 samples excluded* over three rows and put that number three lines above a denominator that disagrees. `by_task` below is unaffected and stays per instance: it is the *errored* cell's split, and classification gives an errored sample exactly one `error:` class.

    Args:
        batches: Detection's full census.
        anomalies: The fold, post-policy — the rulings in force.
        current: Task identifier to its current attempt's log location.
        reused: Per resumed task, the sample uuids its current log holds.

    Returns:
        Bucket counts per task over `error:` instances, the run-wide exclusion and zero totals over every sample-shaped kind, and the samples each class left in the data.
    """
    by_task: dict[str, dict[str, int]] = {}
    marked: dict[str, str] = {}
    for batch in batches:
        # the totals are about rows in the results, so every kind a mark can
        # honestly be recorded against counts toward them — an excluded reward
        # hack is a sample excluded from scoring exactly as an excluded timeout
        # is. `SAMPLE_MARKED` rather than `SAMPLE_SHAPED`: a `scanerror:` class
        # has a sample behind every instance and nothing to exclude
        if batch.kind not in SAMPLE_MARKED:
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
            if not in_results(instance, current, reused):
                continue
            bucket = by_ref.get(instance.ref, "undecided")
            if batch.kind == "error":
                counts = by_task.setdefault(instance.task, {})
                counts[bucket] = counts.get(bucket, 0) + 1
            if bucket in MARKS:
                marked[instance.ref] = _stronger(marked.get(instance.ref), bucket)
    return Dispositions(
        by_task=by_task,
        excluded=sum(1 for mark in marked.values() if mark == "excluded"),
        zeroed=sum(1 for mark in marked.values() if mark == "zeroed"),
        affected=affected_refs(batches, current, reused),
    )


MARKS = ("excluded", "zeroed")
"""The two buckets that move the scoring population, strongest first."""


def _stronger(mark: str | None, bucket: str) -> str:
    """Which of two marks on one sample row is the one the numbers describe.

    **Exclusion wins**, and it is the only answer arithmetic admits: `marks_note` subtracts the exclusions from the denominator, so a row counted as both is a row simultaneously in and out of the population being scored. Once a decision has taken a row out of the scores there is nothing left for a second decision to zero, which makes the precedence a statement about the data rather than a tie-break.

    Two rulings this far apart are worth a person's attention, and they get it where it belongs: both classes appear in `anomalies.md` with their own reasons, and the signature names both as exceptions. What is settled here is only what the one denominator line says.
    """
    return "excluded" if "excluded" in (mark, bucket) else bucket


def _window_bucket(window: Anomaly) -> str:
    """The disposition in force over one window's instances, as a report bucket."""
    if window.state is AnomalyState.RULED:
        return "rerunning"
    if window.open or window.ruling is None:
        return "undecided"
    return BUCKETS.get(window.ruling.disposition, "undecided")


__all__ = [
    "ACCEPTANCE_KEY",
    "BUCKETS",
    "Dispositions",
    "accepted_tasks",
    "apply_rulings",
    "dispositions",
    "policy_rulings",
    "rerun_ruled",
]
