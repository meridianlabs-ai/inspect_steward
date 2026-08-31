"""The anomaly fold, and the absorb step that decides what a turn owes the journal.

Two halves, one discipline. `read_anomalies` replays the journal into `Anomalies` — a pure fold like `read_acks` and `read_pause`, which is what makes crash recovery the ordinary code path and `status` an honest preview. `absorb` diffs a turn's detection against that fold and returns the **delta** as pending events: what is genuinely new, batched one `instance` per class, plus the `opened` and `resolution` edges the deltas imply. The turn appends them on execute; either way the in-memory state is one more fold over `events + as_events(pending)`, so there is exactly one transition function and no second apply path to drift from it.

**Deltas, not totals.** Detection recomputes the full instance census from the log directory every turn, idempotently. The journal absorbs only what the fold has not seen — an exact diff by content-derived ref, whatever the kind — so losing `.steward/classed.json` costs a re-read and double-counts nothing. Refs rather than per-eval counts, deliberately: a count cannot see one sample's failure replaced by another's in the same eval (a requeue's ordinary shape), and a ref diff can.

**Routing after a ruling.** A `rerun` ruling closes its window; what fails afterwards is not more of the same anomaly. A new instance in a *newer* attempt is either the authorized re-run failing again — same sample-in-task, or same task, as the ruled population — which lands as a `reran_failed` resolution on the ruled window, or a genuinely new failure, which opens the next generation carrying the ruling as precedent. A **warm** requeue re-runs in-attempt and never moves the attempt instant, so its outcomes route by the applied-rulings fold instead (`applied.py`): a failure whose target was warm-requeued is `reran_failed` whatever its attempt says, and a warm pass is witnessed by the application record plus an empty `unapplied` remainder. And a `reran_failed` is sticky: the window stays `RULED` and cannot pass mechanically while the census holds instances newer than the ruling or the failure postdates the ruling in force, so only a fresh ruling — a new instant — re-arms the pass.

**Tasks heal mechanically; samples never do.** An un-ruled `task:` window resolves itself (`reran_passed`) once every involved task is `COMPLETE` again and the class has gone quiet — Steward already respawns workers without asking, and a transient death that its own retry absorbed must not gate signoff. Sample, limit, and score windows never auto-resolve: their residue is in the data, and the four-answer question stands until a person rules (workflow.md §12).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from .._evalset.classify import kind_of
from .._evalset.instances import Instance, InstanceBatch
from .._util.duration import is_after as _after
from .._workspace.journal import (
    INSTANCE,
    INVESTIGATING,
    OPENED,
    PROPOSAL,
    RESOLUTION,
    RULING,
    JournalEvent,
)
from .applied import Applied
from .model import (
    ABSORBING,
    TERMINAL,
    Anomalies,
    Anomaly,
    AnomalyState,
    Disposition,
    Evidence,
    Outcome,
    Proposal,
    ProposalEvidence,
    Resolution,
    Ruling,
)

SAMPLE_CAP = 20
"""`id:epoch` pairs carried per `instance` event and per window — enough to go look, never the census (the census re-derives from the logs)."""

LOG_CAP = 20
"""Log locations carried per event and per window. Evidence like the samples; the tasks list is deliberately *not* capped, because resolution detection and post-ruling routing read it for membership."""


@dataclass
class _Window:
    """One window mid-fold — the mutable builder `Anomaly` is frozen from."""

    class_key: str
    kind: str
    substrate: bool
    generation: int
    opened_ts: str
    state: AnomalyState = AnomalyState.OPEN
    count: int = 0
    samples: list[str] = field(default_factory=list[str])
    tasks: list[str] = field(default_factory=list[str])
    logs: list[str] = field(default_factory=list[str])
    exemplar: str = ""
    first_ts: str = ""
    last_ts: str = ""
    note: str = ""
    proposal: str | None = None
    ruling: Ruling | None = None
    resolution: Resolution | None = None
    superseded: list[Ruling] = field(default_factory=list[Ruling])
    failed: int = 0
    refs: set[str] = field(default_factory=set[str])
    failed_refs: dict[str, str] = field(default_factory=dict[str, str])


def read_anomalies(events: list[JournalEvent]) -> Anomalies:
    """Fold a journal down to what its anomalies are.

    Args:
        events: The journal, in file order.

    Returns:
        Every window ever opened — open and settled — with precedent attached, the live proposals, and the dedupe ledger the absorb step diffs against. Payloads this version cannot read are data, not damage: an event missing what its transition needs is skipped, never raised on.
    """
    windows: dict[str, list[_Window]] = {}
    proposals: dict[str, Proposal] = {}
    refs: dict[str, set[str]] = {}
    for event in events:
        if event.type == OPENED:
            _opened(event, windows)
        elif event.type == INSTANCE:
            _instance(event, windows, refs)
        elif event.type == INVESTIGATING:
            _investigating(event, windows)
        elif event.type == PROPOSAL:
            _proposal(event, windows, proposals)
        elif event.type == RULING:
            _ruling(event, windows)
        elif event.type == RESOLUTION:
            _resolution(event, windows, refs)
    return _build(windows, proposals, refs)


def _class_of(event: JournalEvent) -> str | None:
    value = event.payload.get("class")
    return value if isinstance(value, str) and value else None


def _current(windows: dict[str, list[_Window]], class_key: str) -> _Window | None:
    """The window absorbing instances of a class, if one is."""
    for window in reversed(windows.get(class_key, [])):
        if window.state in ABSORBING:
            return window
    return None


def _open_window(
    event: JournalEvent, windows: dict[str, list[_Window]], class_key: str
) -> _Window:
    listed = windows.setdefault(class_key, [])
    kind = event.payload.get("kind")
    window = _Window(
        class_key=class_key,
        kind=kind if isinstance(kind, str) and kind else kind_of(class_key),
        substrate=event.payload.get("substrate") is True,
        generation=len(listed) + 1,
        opened_ts=event.ts,
    )
    listed.append(window)
    return window


def _opened(event: JournalEvent, windows: dict[str, list[_Window]]) -> None:
    class_key = _class_of(event)
    if class_key is None or _current(windows, class_key) is not None:
        return
    _open_window(event, windows, class_key)


def _instance(
    event: JournalEvent,
    windows: dict[str, list[_Window]],
    refs: dict[str, set[str]],
) -> None:
    """Fold one instance batch into its window — by what its refs add, not what it claims.

    The count comes from the refs the ledger has not seen, which makes a replayed event a no-op: a verb persisting a window it is about to decide can land the same batch a concurrent tend also lands, and the second copy must change nothing.
    """
    class_key = _class_of(event)
    if class_key is None:
        return
    listed = event.payload.get("refs")
    fresh = _ledger(class_key, event, refs)
    if isinstance(listed, list) and not fresh:
        return
    window = _current(windows, class_key)
    if window is None:
        # defensively: an `opened` line lost to damage must not lose the batch
        window = _open_window(event, windows, class_key)
    count = event.payload.get("count")
    if isinstance(listed, list):
        window.count += len(fresh)
        # the window's full membership: what a ruling on it covers, and the
        # one set the executor, the pass check, and the report all key on
        window.refs.update(fresh)
    elif isinstance(count, int) and count > 0:
        # an event with no refs at all cannot be diffed, so its count is taken
        # at its word rather than dropped
        window.count += count
    if event.payload.get("substrate") is True:
        # later evidence can reveal the machinery under an already-open class;
        # the flag only ratchets on -- eager by design (execution.md §9.1)
        window.substrate = True
    _extend(window.samples, event.payload.get("samples"), SAMPLE_CAP)
    _extend(window.tasks, event.payload.get("tasks"), None)
    _extend(window.logs, event.payload.get("logs"), LOG_CAP)
    exemplar = event.payload.get("exemplar")
    if not window.exemplar and isinstance(exemplar, str):
        window.exemplar = exemplar
    window.first_ts = window.first_ts or event.ts
    window.last_ts = event.ts


def _extend(into: list[str], listed: object, cap: int | None) -> None:
    if not isinstance(listed, list):
        return
    for value in cast(list[object], listed):
        if not isinstance(value, str) or not value or value in into:
            continue
        if cap is not None and len(into) >= cap:
            return
        into.append(value)


def _ledger(
    class_key: str,
    event: JournalEvent,
    refs: dict[str, set[str]],
) -> list[str]:
    """Accumulate an event's refs into the dedupe ledger, returning the ones that were new.

    Cumulative across windows deliberately: a ruling closes a window, and the same log's errors must not read as news to the next one.
    """
    listed = event.payload.get("refs")
    if not isinstance(listed, list):
        return []
    seen = refs.setdefault(class_key, set())
    fresh: list[str] = []
    for value in cast(list[object], listed):
        if isinstance(value, str) and value not in seen:
            seen.add(value)
            fresh.append(value)
    return fresh


def _investigating(event: JournalEvent, windows: dict[str, list[_Window]]) -> None:
    class_key = _class_of(event)
    window = _current(windows, class_key) if class_key is not None else None
    if window is None:
        return
    window.state = AnomalyState.INVESTIGATING
    note = event.payload.get("note")
    if isinstance(note, str):
        window.note = note
    # investigating a proposed class pulls it back out of the proposal
    window.proposal = None


def _proposal(
    event: JournalEvent,
    windows: dict[str, list[_Window]],
    proposals: dict[str, Proposal],
) -> None:
    identifier = event.payload.get("id")
    action = _disposition(event.payload.get("action"))
    classes = event.payload.get("classes")
    if (
        not isinstance(identifier, str)
        or not identifier
        or action is None
        or not isinstance(classes, dict)
    ):
        return
    evidence = {
        key: _proposal_evidence(raw)
        for key, raw in cast(dict[str, object], classes).items()
    }
    proposals[identifier] = Proposal(
        id=identifier,
        action=action,
        classes=tuple(evidence),
        evidence=evidence,
        reason=_text(event.payload.get("reason")),
        by=_text(event.payload.get("by")),
        ts=event.ts,
    )
    for key in evidence:
        window = _current(windows, key)
        if window is not None:
            # a later proposal covering the class supersedes the earlier one
            window.state = AnomalyState.PROPOSED
            window.proposal = identifier


def _proposal_evidence(raw: object) -> ProposalEvidence:
    if not isinstance(raw, dict):
        return ProposalEvidence()
    record = cast(dict[str, object], raw)
    count = record.get("count")
    precedent = record.get("precedent")
    return ProposalEvidence(
        count=count if isinstance(count, int) else 0,
        exemplar=_text(record.get("exemplar")),
        first_ts=_text(record.get("first_ts")),
        last_ts=_text(record.get("last_ts")),
        precedent=tuple(
            value for value in cast(list[object], precedent) if isinstance(value, str)
        )
        if isinstance(precedent, list)
        else (),
    )


def _ruling(event: JournalEvent, windows: dict[str, list[_Window]]) -> None:
    class_key = _class_of(event)
    disposition = _disposition(event.payload.get("disposition"))
    if class_key is None or disposition is None:
        return
    proposal = event.payload.get("proposal")
    ruling = Ruling(
        class_key=class_key,
        disposition=disposition,
        reason=_text(event.payload.get("reason")),
        by=_text(event.payload.get("by")),
        ts=event.ts,
        proposal=proposal if isinstance(proposal, str) and proposal else None,
        effect=_text(event.payload.get("effect")),
    )
    # class-scoped: every window not yet terminal, so ruling a class with a
    # pending re-run *and* a fresh recurrence answers both, superseding loudly
    for window in windows.get(class_key, []):
        if window.state in TERMINAL:
            continue
        if window.ruling is not None:
            window.superseded.append(window.ruling)
        window.ruling = ruling
        window.state = _ruled_state(disposition)


def _ruled_state(disposition: Disposition) -> AnomalyState:
    if disposition is Disposition.RERUN:
        return AnomalyState.RULED
    if disposition is Disposition.DISMISS:
        return AnomalyState.RESOLVED
    return AnomalyState.ACCEPTED


def _resolution(
    event: JournalEvent,
    windows: dict[str, list[_Window]],
    refs: dict[str, set[str]],
) -> None:
    class_key = _class_of(event)
    outcome = event.payload.get("outcome")
    if class_key is None or not isinstance(outcome, str):
        return
    try:
        parsed = Outcome(outcome)
    except ValueError:
        return
    window = _resolvable(windows.get(class_key, []))
    if window is None:
        return
    window.resolution = Resolution(
        outcome=parsed, detail=_text(event.payload.get("detail")), ts=event.ts
    )
    if parsed is Outcome.RERAN_PASSED:
        window.state = AnomalyState.RESOLVED
        _ledger(class_key, event, refs)
    else:
        window.failed += 1
        # a reran_failed carries the instances it consumed, so they are
        # absorbed here rather than re-counted as news next turn -- and they
        # join the window's own record with this instant, because a *later*
        # ruling on the window covers them where the one that authorized the
        # failed re-run never may (`covered_refs`)
        for ref in _ledger(class_key, event, refs):
            window.failed_refs[ref] = event.ts


def _resolvable(listed: list[_Window]) -> _Window | None:
    """The window a resolution lands on: the pending re-run when there is one — even with a fresh recurrence absorbing beside it — else the newest absorbing window (the mechanical heal of an un-ruled task class)."""
    for window in reversed(listed):
        if window.state is AnomalyState.RULED:
            return window
    for window in reversed(listed):
        if window.state in ABSORBING:
            return window
    return None


def _disposition(value: object) -> Disposition | None:
    if not isinstance(value, str):
        return None
    try:
        return Disposition(value)
    except ValueError:
        return None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _build(
    windows: dict[str, list[_Window]],
    proposals: dict[str, Proposal],
    refs: dict[str, set[str]],
) -> Anomalies:
    open_windows: list[Anomaly] = []
    settled: list[Anomaly] = []
    live: set[str] = set()
    for listed in windows.values():
        precedent: list[Ruling] = []
        for window in listed:
            if window.count == 0 and window.state is AnomalyState.OPEN:
                # an `opened` whose instances never landed -- a torn write, or
                # a stale replay beside a concurrent writer. Invisible rather
                # than an empty question: if instances exist they were never
                # absorbed, so a later turn re-emits them and the window fills
                continue
            anomaly = _anomaly(window, tuple([*precedent, *window.superseded]))
            (settled if window.state in TERMINAL else open_windows).append(anomaly)
            if window.state is AnomalyState.PROPOSED and window.proposal:
                live.add(window.proposal)
            precedent.extend(window.superseded)
            if window.ruling is not None:
                precedent.append(window.ruling)
    return Anomalies(
        open=_ordered(open_windows),
        settled=_ordered(settled),
        proposals={
            identifier: proposal
            for identifier, proposal in proposals.items()
            if identifier in live
        },
        absorbed_refs={class_key: frozenset(seen) for class_key, seen in refs.items()},
    )


def _ordered(listed: list[Anomaly]) -> tuple[Anomaly, ...]:
    return tuple(
        sorted(listed, key=lambda anomaly: (anomaly.class_key, anomaly.generation))
    )


def _anomaly(window: _Window, precedent: tuple[Ruling, ...]) -> Anomaly:
    return Anomaly(
        class_key=window.class_key,
        kind=window.kind,
        state=window.state,
        evidence=Evidence(
            count=window.count,
            samples=tuple(window.samples),
            tasks=tuple(window.tasks),
            logs=tuple(window.logs),
            exemplar=window.exemplar,
            first_ts=window.first_ts,
            last_ts=window.last_ts,
        ),
        substrate=window.substrate,
        generation=window.generation,
        opened_ts=window.opened_ts,
        note=window.note,
        proposal=window.proposal if window.state is AnomalyState.PROPOSED else None,
        ruling=window.ruling,
        resolution=window.resolution,
        precedent=precedent,
        failed_resolutions=window.failed,
        refs=frozenset(window.refs),
        failed_refs=dict(window.failed_refs),
    )


@dataclass(frozen=True)
class Pending:
    """One event a turn owes the journal, computed before anything is written.

    A tend appends these on execute; a `status` discards them. Both fold `as_events` of the same list into the same state, which is what makes the preview honest.
    """

    type: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class TaskHealth:
    """One task's recovery, as the turn reads it from the schedule — what resolution detection consumes."""

    complete: bool
    """The task is `COMPLETE`: a successful current attempt, nothing pending."""

    settled: str = ""
    """The current attempt's `created`, for ordering against a ruling — a sample-kind re-run must land as a *new* attempt to count as the ruling's outcome."""


def absorb(
    anomalies: Anomalies,
    batches: Sequence[InstanceBatch],
    health: Mapping[str, TaskHealth],
    applied: Applied | None = None,
) -> list[Pending]:
    """Diff a turn's detection against the fold: the events this turn owes.

    Args:
        anomalies: The fold of the journal as read this turn.
        batches: Detection's full census, one batch per class.
        health: Per task, whether it stands recovered — what turns a pending re-run or a respawned worker into a resolution.
        applied: The applied-rulings fold, for the warm boundary: a warm requeue never moves the attempt's `created`, so its outcomes are routed and passed by the application record rather than by attempt instants. Omitted, nothing warm was ever applied.

    Returns:
        Pending `opened`/`instance`/`resolution` events, ordered so folding them after the journal is well-defined (a class's `reran_failed` precedes the `opened` of its next generation).
    """
    applied = applied if applied is not None else Applied()
    pending: list[Pending] = []
    active: set[str] = set()
    present = {batch.class_key: batch.instances for batch in batches}
    for batch in batches:
        new = _new(anomalies, batch)
        if not new:
            continue
        active.add(batch.class_key)
        ruled = _pending_rerun(anomalies, batch.class_key)
        if ruled is not None and ruled.ruling is not None:
            failed, new = _routed(ruled, ruled.ruling, batch, new, applied)
            if failed:
                pending.append(_reran_failed(ruled, failed))
        if new:
            if anomalies.absorbing(batch.class_key) is None:
                pending.append(
                    Pending(
                        type=OPENED,
                        fields={
                            "class": batch.class_key,
                            "kind": batch.kind,
                            "substrate": batch.substrate,
                        },
                    )
                )
            pending.append(_instance_pending(batch, new))
    for anomaly in anomalies.open:
        passed = _passed(anomaly, health, active, present, applied)
        if passed is not None:
            pending.append(passed)
    return pending


def covered_refs(anomaly: Anomaly, ruling_ts: str) -> frozenset[str]:
    """The population a ruling at this instant covers.

    The window's absorbed instances, plus the re-run failures recorded against it **at or before the instant**. The boundary is what keeps both loops closed: the ruling that authorized a re-run never covers that re-run's own failure (re-applying it would be an unruled re-run), while a later ruling — made with the failure on the record — covers exactly it, which is what makes re-ruling after `reran_failed` actually re-run the failed sample.
    """
    return anomaly.refs | frozenset(
        ref for ref, ts in anomaly.failed_refs.items() if not _after(ts, ruling_ts)
    )


def unapplied(
    instances: Sequence[Instance],
    class_key: str,
    ruling_ts: str,
    applied: Applied,
    refs: frozenset[str],
) -> list[Instance]:
    """The ruled population not yet acted on — one membership definition, three readers.

    The ruled population is every census instance the ruled window absorbed — its `refs`, the only set the ruling actually covered. An attempt instant cannot draw that line: a failure appearing *after* the ruling inside the same still-running attempt predates nothing, joins the next generation, and must never be re-run under a decision that never saw it. What has been acted on comes off the applied fold: warm targets by `(task, sample_id, epoch)`, invalidations by uuid.

    Three readers, deliberately one function: `_tend.rulings` applies exactly this remainder, `_passed`'s warm branch requires it empty, and their agreeing is what makes "fully applied" safely derivable instead of stored.
    """
    warm = applied.warm_targets(class_key, ruling_ts)
    invalidated = applied.invalidated_uuids(class_key, ruling_ts)
    return [
        instance
        for instance in instances
        if instance.ref in refs
        and (instance.task, instance.sample_id, instance.epoch) not in warm
        and not (instance.uuid and instance.uuid in invalidated)
    ]


def as_events(pending: Sequence[Pending], ts: str) -> list[JournalEvent]:
    """The pending events as journal events, so state-if-executed is one more fold rather than a second apply path."""
    return [
        JournalEvent.model_validate({"ts": ts, "type": entry.type, **entry.fields})
        for entry in pending
    ]


def _new(anomalies: Anomalies, batch: InstanceBatch) -> list[Instance]:
    """The instances the journal has not absorbed — an exact ref diff, whatever the kind."""
    seen = anomalies.absorbed_refs.get(batch.class_key, frozenset())
    return [instance for instance in batch.instances if instance.ref not in seen]


def _pending_rerun(anomalies: Anomalies, class_key: str) -> Anomaly | None:
    """The class's window awaiting a re-run's outcome, if one is."""
    ruled = [
        anomaly
        for anomaly in anomalies.open
        if anomaly.class_key == class_key
        and anomaly.state is AnomalyState.RULED
        and anomaly.ruling is not None
        and anomaly.ruling.disposition is Disposition.RERUN
    ]
    return ruled[-1] if ruled else None


def _routed(
    ruled: Anomaly,
    ruling: Ruling,
    batch: InstanceBatch,
    new: list[Instance],
    applied: Applied,
) -> tuple[list[Instance], list[Instance]]:
    """Split a class's new instances into the re-run's failures and the genuinely fresh.

    An instance in an attempt newer than the ruling whose sample-in-task (or task) also failed in the ruled population is the authorized re-run failing again. Sample membership carries the task identity because sample ids repeat across tasks, and comes from the census instances the ruled window's `refs` name — the errored logs are still in the directory, so the ruled population reads straight off it — while task membership comes from the window's evidence, because a reaped worker's instances leave the census the turn after they are noticed. An instance that is not the re-run of a ruled sample — a log that surfaced late, a fresh failure the ruling never saw — is fresh either way: it joins the next generation with the ruling attached as precedent rather than reopening a closed window.

    **A warm re-run's failure never moves the attempt instant**, so it routes by the applied fold instead: a requeue re-runs the sample under a fresh uuid inside the same attempt, and a new instance whose `(task, sample_id, epoch)` was warm-requeued for this ruling is the re-run failing again whatever its `attempt_created` says.

    A `score` window routes like a task window, by evidence-task membership: its class key is already task-scoped and its instance is the whole attempt, so another all-zero result in a newer attempt of a ruled task *is* the authorized re-run failing again — and its ref names the fresh attempt, which is exactly why census ref membership cannot recognize it (the invalidated attempt's ref has left the census).
    """
    if batch.kind in ("task", "score"):
        member = set(ruled.evidence.tasks)
        failed = [
            instance
            for instance in new
            if _after(instance.attempt_created, ruling.ts) and instance.task in member
        ]
    else:
        warm = applied.warm_targets(ruled.class_key, ruling.ts)
        covered = covered_refs(ruled, ruling.ts)
        membership = {
            (instance.task, instance.sample_id, instance.epoch)
            for instance in batch.instances
            if instance.ref in covered
        }
        failed = [
            instance
            for instance in new
            if (instance.task, instance.sample_id, instance.epoch) in warm
            or (
                _after(instance.attempt_created, ruling.ts)
                and (instance.task, instance.sample_id, instance.epoch) in membership
            )
        ]
    consumed = {instance.ref for instance in failed}
    fresh = [instance for instance in new if instance.ref not in consumed]
    return failed, fresh


def _reran_failed(ruled: Anomaly, failed: list[Instance]) -> Pending:
    fields: dict[str, Any] = {
        "class": ruled.class_key,
        "outcome": Outcome.RERAN_FAILED.value,
        "detail": (
            f"{len(failed)} of {ruled.evidence.count} failed again after the re-run"
        ),
    }
    fields.update(_ledger_fields(failed))
    return Pending(type=RESOLUTION, fields=fields)


def _instance_pending(batch: InstanceBatch, new: list[Instance]) -> Pending:
    fields: dict[str, Any] = {
        "class": batch.class_key,
        "kind": batch.kind,
        "substrate": batch.substrate,
        "count": len(new),
    }
    fields.update(_ledger_fields(new))
    samples = [
        f"{instance.sample_id}:{instance.epoch}"
        for instance in new
        if instance.sample_id
    ][:SAMPLE_CAP]
    tasks = sorted({instance.task for instance in new if instance.task})
    logs = sorted({instance.location for instance in new if instance.location})[
        :LOG_CAP
    ]
    exemplar = next((instance.message for instance in new if instance.message), "")
    if samples:
        fields["samples"] = samples
    if tasks:
        fields["tasks"] = tasks
    if logs:
        fields["logs"] = logs
    if exemplar:
        fields["exemplar"] = exemplar
    return Pending(type=INSTANCE, fields=fields)


def _ledger_fields(instances: list[Instance]) -> dict[str, Any]:
    refs = [instance.ref for instance in instances]
    return {"refs": refs} if refs else {}


def _passed(
    anomaly: Anomaly,
    health: Mapping[str, TaskHealth],
    active: set[str],
    present: Mapping[str, tuple[Instance, ...]],
    applied: Applied,
) -> Pending | None:
    """Whether a window resolves clean this turn.

    Two cases share the check. A `rerun`-ruled window passes when every involved task stands `COMPLETE` and the class produced nothing new — for sample-kind classes each task's recovery must be an attempt *newer than the ruling*, **or** a warm application with nothing left unapplied (a requeue re-runs in-attempt, so the attempt instant cannot witness it; the applied fold does) — while a task-kind recovery may legitimately be the very attempt that was underway, resumed. An un-ruled `task:` window passes on the same evidence without any ruling: the module docstring's mechanical heal.

    The boundary is **per task**: a class spanning two tasks needs each one recovered on its own evidence, and a warm application on one must not excuse the other's missing new attempt.

    Two refusals guard a ruled window. It never passes while the census still holds an instance from an attempt newer than the ruling — the re-run's own failures, already absorbed and journaled as `reran_failed`. And it never passes while its newest `reran_failed` resolution postdates the ruling in force: a warm failure's `attempt_created` is old and its errored record can later be replaced by a passing manual re-run, so in that trap this refusal is the only one standing. A fresh ruling — a new instant — re-arms both.
    """
    if anomaly.class_key in active:
        return None
    tasks = anomaly.evidence.tasks
    if not tasks:
        return None
    ruling = anomaly.ruling
    if (
        anomaly.state is AnomalyState.RULED
        and ruling is not None
        and ruling.disposition is Disposition.RERUN
    ):
        instances = present.get(anomaly.class_key, ())
        if any(_after(one.attempt_created, ruling.ts) for one in instances):
            return None
        resolution = anomaly.resolution
        if (
            resolution is not None
            and resolution.outcome is Outcome.RERAN_FAILED
            and _after(resolution.ts, ruling.ts)
        ):
            return None
        boundary = "" if anomaly.kind == "task" else ruling.ts
        detail = "the re-run completed and the class stayed quiet"
    elif anomaly.kind == "task" and anomaly.state in ABSORBING:
        instances = ()
        boundary = ""
        detail = "the failed attempts were superseded without a ruling"
    else:
        return None
    for identifier in tasks:
        state = health.get(identifier)
        if state is None or not state.complete:
            return None
        if not boundary or ruling is None:
            continue
        if _after(state.settled, boundary):
            # landed: the recovery is a genuinely new attempt
            continue
        # warm: the ruling was applied to this task, after it was made, and
        # nothing of its ruled population is left unacted-on
        witness = applied.witness(anomaly.class_key, ruling.ts, identifier)
        if witness is None or not _after(witness, ruling.ts):
            return None
        if any(
            one.task == identifier
            for one in unapplied(
                instances,
                anomaly.class_key,
                ruling.ts,
                applied,
                covered_refs(anomaly, ruling.ts),
            )
        ):
            return None
    return Pending(
        type=RESOLUTION,
        fields={
            "class": anomaly.class_key,
            "outcome": Outcome.RERAN_PASSED.value,
            "detail": detail,
        },
    )


__all__ = [
    "LOG_CAP",
    "SAMPLE_CAP",
    "Pending",
    "TaskHealth",
    "absorb",
    "as_events",
    "covered_refs",
    "read_anomalies",
    "unapplied",
]
