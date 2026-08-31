"""Applying rerun rulings: warm requeues, landed invalidations, and standing grants.

Two layers, split by what can be manufactured. The landed half runs through real turns against real logs — the invalidation is written, read back, and its provenance checked in the file, because the file is the effect. The warm half cannot be (no eval is reliably mid-run on demand), so it drives `apply_rulings` directly with a recorded `requeue_sample` — which is also where the memory claims live: a partial application journals exactly what landed and retries exactly the remainder, and nothing ever stores "fully applied".
"""

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.log import read_eval_log
from inspect_steward._anomaly.applied import RULING_APPLIED, read_applied
from inspect_steward._anomaly.model import (
    Anomalies,
    Anomaly,
    AnomalyState,
    Disposition,
    Evidence,
    Ruling,
)
from inspect_steward._evalset.instances import Instance, InstanceBatch
from inspect_steward._evalset.observe import (
    LogAttempt,
    ObservedTasks,
    TaskObservation,
    TaskState,
    UnreadableLog,
)
from inspect_steward._schedule import InFlight
from inspect_steward._tend import status, status_markdown
from inspect_steward._tend.rulings import (
    apply_rulings,
    dispositions,
    policy_rulings,
    rerun_ruled,
)
from inspect_steward._worker import LiveFleet, LiveTask, RequeueView, Unavailable
from inspect_steward._workspace import (
    ACTION,
    PAUSED,
    RULING,
    Workspace,
    append_event,
    read_journal,
)

from .._logs import SynthSample, SynthTask, write_log
from ..schedule.test_reconcile import live
from ..schedule.test_tend import prepared, turn
from .test_items import TIMEOUT_TRACEBACK, erroring, ruling

CLASS = "error:openai.APITimeoutError@openai/_client.py:post"
IDENT = "task-one"
CREATED = "2026-08-30T10:00:00Z"
RULED_AT = "2026-08-31T00:00:00Z"


# --- the unit layer's fixtures --------------------------------------------


def window(
    *,
    state: AnomalyState = AnomalyState.RULED,
    disposition: Disposition = Disposition.RERUN,
    ts: str = RULED_AT,
    tasks: tuple[str, ...] = (IDENT,),
    kind: str = "error",
    substrate: bool = False,
    generation: int = 1,
    failed_resolutions: int = 0,
    refs: frozenset[str] = frozenset(),
) -> Anomaly:
    return Anomaly(
        class_key=CLASS,
        kind=kind,
        state=state,
        evidence=Evidence(count=1, tasks=tasks),
        substrate=substrate,
        generation=generation,
        failed_resolutions=failed_resolutions,
        refs=refs,
        ruling=Ruling(
            class_key=CLASS,
            disposition=disposition,
            reason="transient",
            by="kaia",
            ts=ts,
        )
        if state in (AnomalyState.RULED, AnomalyState.ACCEPTED)
        else None,
    )


def over(*instances: Instance, **kwargs: Any) -> Anomaly:
    """A window that absorbed exactly these instances, as the fold would build it."""
    return window(refs=frozenset(one.ref for one in instances), **kwargs)


def inst(sample_id: str, *, task: str = IDENT, epoch: int = 1) -> Instance:
    return Instance(
        class_key=CLASS,
        ref=f"e1:{sample_id}:{epoch}:u-{sample_id}",
        task=task,
        location="logs/a.eval",
        attempt_created=CREATED,
        eval_id="e1",
        sample_id=sample_id,
        epoch=epoch,
        uuid=f"u-{sample_id}",
    )


def census(*instances: Instance) -> list[InstanceBatch]:
    return [
        InstanceBatch(
            class_key=CLASS, kind="error", substrate=False, instances=instances
        )
    ]


@dataclass
class FakeActed:
    failures: list[str] = field(default_factory=list[str])
    journalled: bool = False


def workspace_at(root: Path) -> Workspace:
    workspace, _ = prepared(root, [])
    workspace.journal.parent.mkdir(parents=True, exist_ok=True)
    return workspace


def apply(
    workspace: Workspace,
    batches: list[InstanceBatch],
    *,
    anomalies: Anomalies | None = None,
    running: tuple[str, ...] = (IDENT,),
    fleet: LiveFleet | None = None,
    spawned: set[str] | None = None,
    observed: ObservedTasks | None = None,
) -> FakeActed:
    acted = FakeActed()
    if anomalies is None:
        # the window absorbed the census, as a real fold would have
        anomalies = Anomalies(
            open=(over(*(one for batch in batches for one in batch.instances)),)
        )
    apply_rulings(
        workspace,
        anomalies,
        batches,
        read_applied(read_journal(workspace.journal).events),
        InFlight(running=[live(identifier) for identifier in running]),
        fleet
        if fleet is not None
        else LiveFleet(tasks={IDENT: LiveTask(pid=1, identifier=IDENT, task_id="T1")}),
        observed if observed is not None else ObservedTasks(tasks=[]),
        spawned or set(),
        acted,
    )
    return acted


def applications(workspace: Workspace) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == ACTION and event.payload.get("action") == RULING_APPLIED
    ]


def accepted() -> RequeueView:
    return RequeueView.model_validate(
        {"applied": True, "detail": {"changed": True, "status": "error"}}
    )


def already_coming() -> RequeueView:
    return RequeueView.model_validate(
        {"applied": False, "detail": {"changed": False, "status": "queued"}}
    )


# --- the warm path ---------------------------------------------------------


def test_a_partial_application_records_what_landed_and_retries_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 409 defers with no record, so a stored "fully applied" can never lie.

    The first turn lands one of two targets; the journal must say exactly that,
    and the second turn must requeue exactly the other one.
    """
    workspace = workspace_at(tmp_path)
    calls: list[tuple[str, str, int]] = []
    outcomes = [accepted(), Unavailable("http_error", "409: finishing")]

    def requeue(task_id: str, sample_id: str, epoch: int) -> RequeueView | Unavailable:
        calls.append((task_id, sample_id, epoch))
        return outcomes[len(calls) - 1]

    monkeypatch.setattr("inspect_steward._tend.rulings.requeue_sample", requeue)
    apply(workspace, census(inst("s1"), inst("s2")))

    (first,) = applications(workspace)
    assert first["requeued"] == [{"task": IDENT, "id": "s1", "epoch": 1}]
    assert [entry["id"] for entry in first["deferred"]] == ["s2"]
    fold = read_applied(read_journal(workspace.journal).events)
    assert fold.warm_targets(CLASS, RULED_AT) == {(IDENT, "s1", 1)}

    outcomes.append(accepted())
    apply(workspace, census(inst("s1"), inst("s2")))

    # exactly the remainder: s1 is applied memory, never re-requeued
    assert calls[2:] == [("T1", "s2", 1)]
    fold = read_applied(read_journal(workspace.journal).events)
    assert fold.warm_targets(CLASS, RULED_AT) == {(IDENT, "s1", 1), (IDENT, "s2", 1)}


def test_a_requeue_already_coming_books_as_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # deferring the no-op would requeue the re-run's own later failure -- an
    # unruled re-run -- so `changed: false` is memory, not a deferral
    workspace = workspace_at(tmp_path)

    def requeue(task_id: str, sample_id: str, epoch: int) -> RequeueView | Unavailable:
        return already_coming()

    monkeypatch.setattr("inspect_steward._tend.rulings.requeue_sample", requeue)

    apply(workspace, census(inst("s1")))

    (event,) = applications(workspace)
    assert event["requeued"] == [{"task": IDENT, "id": "s1", "epoch": 1}]
    assert "deferred" not in event


def test_nothing_left_and_no_witness_is_convergence_worth_recording(
    tmp_path: Path,
) -> None:
    """A human requeued by hand, or upstream retries absorbed the errors.

    Without this record the pass check could never credit the recovery and the
    window would stick RULED forever. And it is written once: the witness it
    creates suppresses a second one.
    """
    workspace = workspace_at(tmp_path)

    apply(workspace, census())
    apply(workspace, census())

    (event,) = applications(workspace)
    assert event["converged"] == [IDENT]
    fold = read_applied(read_journal(workspace.journal).events)
    assert fold.witness(CLASS, RULED_AT, IDENT) is not None


def attempt(location: str = "logs/a.eval") -> LogAttempt:
    return LogAttempt(
        location=location,
        identifier=IDENT,
        created=CREATED,
        status="success",
        invalidated=False,
        error=None,
        total_samples=2,
        completed_samples=1,
        epochs=1,
        task="task-one",
        task_id="T1",
        eval_id="e1",
        mtime=None,
    )


def test_an_unreadable_census_never_writes_the_converged_witness(
    tmp_path: Path,
) -> None:
    """An empty remainder is only convergence when the census is authoritative.

    A missing batch can also mean the task's summaries would not read, and a converged record written over that blindness is a false witness: the pass check trusts it, resolves `reran_passed` with nothing re-run, and the absorbed refs never reopen once the log reads again. Blindness defers with no record — and the convergence lands the turn the read recovers.
    """
    workspace = workspace_at(tmp_path)
    observation = TaskObservation(
        identifier=IDENT,
        state=TaskState.COMPLETE,
        task=None,
        current=attempt(),
    )
    blinded = ObservedTasks(
        tasks=[observation],
        unreadable=[
            UnreadableLog(location="logs/a.eval", reason="summaries truncated")
        ],
    )
    ruled = Anomalies(open=(over(inst("s1")),))

    apply(workspace, [], anomalies=ruled, running=(), observed=blinded)
    assert applications(workspace) == []

    apply(
        workspace,
        [],
        anomalies=ruled,
        running=(),
        observed=ObservedTasks(tasks=[observation]),
    )
    (event,) = applications(workspace)
    assert event["converged"] == [IDENT]


def test_an_unanswering_worker_defers_with_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the `_signals` skip triple: missing, unavailable, or no task id -- the
    # remainder is recomputed next turn, so a deferral must leave no memory
    workspace = workspace_at(tmp_path)

    def requeue(task_id: str, sample_id: str, epoch: int) -> RequeueView | Unavailable:
        pytest.fail("an unanswering worker must not be asked")

    monkeypatch.setattr("inspect_steward._tend.rulings.requeue_sample", requeue)
    silent = LiveFleet(tasks={IDENT: LiveTask(pid=1, identifier=IDENT, task_id="")})

    acted = apply(workspace, census(inst("s1")), fleet=silent)

    assert applications(workspace) == []
    assert acted.failures == []
    # deferred-only turns explain themselves in the operational log instead
    assert "deferred" in workspace.log.read_text(encoding="utf-8")


def test_a_task_a_worker_was_just_spawned_for_is_left_alone(
    tmp_path: Path,
) -> None:
    # the one race the executor could create: rewriting a log the spawn is
    # about to open for resume. Waiting a turn costs nothing
    workspace = workspace_at(tmp_path)

    apply(workspace, census(inst("s1")), running=(), spawned={IDENT})

    assert applications(workspace) == []


# --- the landed path, through real turns -----------------------------------


def invalidating_run(tmp_path: Path) -> Workspace:
    """An errored run, paused (so nothing respawns), with a rerun ruled."""
    workspace = erroring(tmp_path)
    turn(workspace)
    append_event(workspace.journal, PAUSED, by="test", reason="hold the respawn")
    ruling(workspace, "rerun")
    return workspace


def test_a_landed_log_is_reopened_with_the_ruling_s_provenance(
    tmp_path: Path,
) -> None:
    workspace = invalidating_run(tmp_path)

    turn(workspace)

    (event,) = applications(workspace)
    (entry,) = event["invalidated"]
    assert len(entry["uuids"]) == 2
    log = read_eval_log(entry["location"])
    assert log.invalidated is True
    marked = [
        sample for sample in (log.samples or []) if sample.invalidation is not None
    ]
    assert len(marked) == 2
    assert {sample.invalidation.author for sample in marked if sample.invalidation} == {
        "kaia"
    }
    assert {sample.invalidation.reason for sample in marked if sample.invalidation} == {
        "decided"
    }


READ_TIMEOUT_TRACEBACK = """Traceback (most recent call last):
  File "/venv/lib/python3.13/site-packages/httpx/_client.py", line 101, in send
    raise ReadTimeout("read timed out")
httpx.ReadTimeout: read timed out.
"""


def test_one_class_s_invalidation_is_never_another_s(tmp_path: Path) -> None:
    """The log's `invalidated` header says *some* sample was — never *these*.

    Two classes share a landed log; class A's ruling invalidates its samples
    and flips the header. Class B's later ruling must still write B's own
    invalidation — booking it off the header would record an application whose
    samples were never touched.
    """
    task = SynthTask("probe", samples=6)
    workspace, _ = prepared(tmp_path, [task])
    write_log(
        workspace.logs,
        task,
        completed=2,
        samples=[
            SynthSample(
                id="a1", error="APITimeoutError('a1')", traceback=TIMEOUT_TRACEBACK
            ),
            SynthSample(
                id="a2", error="APITimeoutError('a2')", traceback=TIMEOUT_TRACEBACK
            ),
            SynthSample(
                id="b1", error="ReadTimeout('b1')", traceback=READ_TIMEOUT_TRACEBACK
            ),
            SynthSample(
                id="b2", error="ReadTimeout('b2')", traceback=READ_TIMEOUT_TRACEBACK
            ),
        ],
    )
    opened = turn(workspace)
    keys = sorted(anomaly.class_key for anomaly in opened.anomalies.open)
    assert len(keys) == 2 and CLASS in keys, "the premise of this test"
    other = next(key for key in keys if key != CLASS)
    append_event(workspace.journal, PAUSED, by="test", reason="hold the respawn")

    ruling(workspace, "rerun")
    turn(workspace)
    first = applications(workspace)
    assert [event["class"] for event in first] == [CLASS]

    decision: dict[str, Any] = {
        "class": other,
        "disposition": "rerun",
        "reason": "also transient",
        "by": "rowan",
    }
    append_event(workspace.journal, RULING, **decision)
    turn(workspace)

    events = applications(workspace)
    assert [event["class"] for event in events] == [CLASS, other]
    (entry,) = events[1]["invalidated"]
    assert "note" not in entry
    log = read_eval_log(entry["location"])
    marked = {
        str(sample.id): sample.invalidation
        for sample in (log.samples or [])
        if sample.invalidation is not None
    }
    assert set(marked) == {"a1", "a2", "b1", "b2"}
    assert marked["b1"] is not None and marked["b1"].author == "rowan"


def test_a_second_turn_does_not_reapply(tmp_path: Path) -> None:
    # "fully applied" is derived -- the applicable census minus the fold is
    # empty -- so a repeat turn finds nothing to do and records nothing
    workspace = invalidating_run(tmp_path)
    turn(workspace)

    turn(workspace)

    assert len(applications(workspace)) == 1


def test_a_fresh_ruling_owes_and_receives_a_fresh_application(
    tmp_path: Path,
) -> None:
    workspace = invalidating_run(tmp_path)
    turn(workspace)
    first = [
        event.ts
        for event in read_journal(workspace.journal).events
        if event.type == RULING
    ][-1]

    time.sleep(0.01)
    ruling(workspace, "rerun", reason="try again")
    second = [
        event.ts
        for event in read_journal(workspace.journal).events
        if event.type == RULING
    ][-1]
    assert second != first, "the premise of this test"
    turn(workspace)

    # the new instant holds no records, so the ruling is applied afresh -- and
    # witnessed for the evidence task, which is what the pass check will read
    events = applications(workspace)
    assert len(events) == 2
    assert events[1]["for"] == second
    fold = read_applied(read_journal(workspace.journal).events)
    task = events[1].get("converged", [None])[0] or events[1]["invalidated"][0]["task"]
    assert fold.witness(CLASS, second, task) is not None


# --- standing grants -------------------------------------------------------


def absorbing(**kwargs: Any) -> Anomalies:
    fields: dict[str, Any] = {"state": AnomalyState.OPEN, **kwargs}
    return Anomalies(open=(window(**fields),))


def test_a_matching_pattern_becomes_an_ordinary_ruling_by_policy() -> None:
    pending, notes = policy_rulings(absorbing(), {"error:*Timeout*": "rerun"})

    (entry,) = pending
    assert entry.type == RULING
    assert entry.fields["class"] == CLASS
    assert entry.fields["disposition"] == "rerun"
    assert entry.fields["by"] == "policy"
    assert "error:*Timeout*" in entry.fields["reason"]
    assert notes == []


def test_the_first_pattern_wins_in_file_order() -> None:
    pending, _ = policy_rulings(
        absorbing(), {"error:*": "exclude", "error:*Timeout*": "rerun"}
    )

    (entry,) = pending
    assert entry.fields["disposition"] == "exclude"
    # a marking disposition carries its composed effect, as `steward rule` would
    assert entry.fields["effect"]


def test_a_substrate_class_is_never_rerun_by_a_standing_grant() -> None:
    pending, notes = policy_rulings(absorbing(substrate=True), {"error:*": "rerun"})

    assert pending == []
    (note,) = notes
    assert "machinery" in note


def test_a_failed_rerun_precedent_stops_the_pattern() -> None:
    # after a `reran_failed` a person must look, or policy re-runs every fresh
    # generation forever
    history = Anomalies(
        open=(window(state=AnomalyState.OPEN, generation=2),),
        settled=(
            window(state=AnomalyState.ACCEPTED, generation=1, failed_resolutions=1),
        ),
    )

    pending, notes = policy_rulings(history, {"error:*": "rerun"})

    assert pending == []
    (note,) = notes
    assert "already failed" in note


def test_a_pending_authorized_rerun_makes_the_pattern_wait_silently() -> None:
    # a class-scoped ruling now would supersede the one in flight and
    # re-trigger application -- waiting costs a turn and no note, because
    # nothing was declined
    both = Anomalies(
        open=(
            window(state=AnomalyState.RULED, generation=1),
            window(state=AnomalyState.OPEN, generation=2),
        )
    )

    pending, notes = policy_rulings(both, {"error:*": "rerun"})

    assert (pending, notes) == ([], [])


def test_a_grant_the_kind_cannot_honestly_carry_is_declined() -> None:
    pending, notes = policy_rulings(absorbing(kind="task"), {"error:*": "exclude"})

    assert pending == []
    (note,) = notes
    assert "cannot mark" in note


def test_a_standing_grant_lands_and_applies_in_the_same_turn(
    tmp_path: Path,
) -> None:
    workspace = erroring(tmp_path)
    turn(workspace)
    append_event(workspace.journal, PAUSED, by="test", reason="hold the respawn")

    result = turn(workspace, preauthorized={"error:*Timeout*": "rerun"})

    rulings = [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == RULING
    ]
    (recorded,) = rulings
    assert recorded["by"] == "policy"
    assert len(applications(workspace)) == 1
    (ruled,) = result.anomalies.open
    assert ruled.state is AnomalyState.RULED

    # the application names the ruling the journal actually holds: the refold
    # comes from the file, so `for` matches the instant `append_event` stamped
    # -- a re-synthesized timestamp would miss, and the next turn would apply
    # the same ruling again
    journalled = [
        event.ts
        for event in read_journal(workspace.journal).events
        if event.type == RULING
    ]
    assert applications(workspace)[0]["for"] == journalled[0]
    turn(workspace)
    assert len(applications(workspace)) == 1


def test_status_previews_a_standing_grant_without_recording_it(
    tmp_path: Path,
) -> None:
    """The preview shows the decision the next tend will record — and journals nothing.

    Without the shared-path fold, `status` reported a class the file had already answered as an open question, and a reader acted on a state the very next tend would contradict.
    """
    workspace = erroring(tmp_path)

    result = status(workspace, preauthorized={"error:*Timeout*": "rerun"})

    (ruled,) = result.anomalies.open
    assert ruled.state is AnomalyState.RULED
    assert ruled.ruling is not None and ruled.ruling.by == "policy"
    rulings = [
        event
        for event in read_journal(workspace.journal).events
        if event.type == RULING
    ]
    assert rulings == []


def test_false_declines_the_files_standing_grants(tmp_path: Path) -> None:
    """`--preauthorized false` holds every grant the file would apply, for one turn.

    The second turn proves the grant was live all along: with nothing declining it, the same file records the ruling.
    """
    workspace = erroring(tmp_path)
    workspace.directives.write_text(
        "preauthorized:\n  'error:*Timeout*': rerun\n", encoding="utf-8"
    )
    workspace.journal.parent.mkdir(parents=True, exist_ok=True)
    append_event(workspace.journal, PAUSED, by="test", reason="hold the respawn")

    declined = turn(workspace, preauthorized=False)
    rulings = [
        event
        for event in read_journal(workspace.journal).events
        if event.type == RULING
    ]
    assert rulings == []
    (window,) = declined.anomalies.open
    assert window.state is not AnomalyState.RULED

    granted = turn(workspace)
    (recorded,) = [
        event
        for event in read_journal(workspace.journal).events
        if event.type == RULING
    ]
    assert recorded.payload["by"] == "policy"
    (ruled,) = granted.anomalies.open
    assert ruled.state is AnomalyState.RULED


def test_re_ruling_after_a_failed_rerun_reruns_the_failed_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh ruling's population includes the recorded re-run failure.

    The re-run's failure superseded the record the first ruling covered, so it lives in `failed_refs` rather than `refs` — and a ruling made after it must act on it, or nothing re-runs and the window can pass with the sample still failed.
    """
    workspace = workspace_at(tmp_path)
    calls: list[tuple[str, str, int]] = []

    def requeue(task_id: str, sample_id: str, epoch: int) -> RequeueView | Unavailable:
        calls.append((task_id, sample_id, epoch))
        return accepted()

    monkeypatch.setattr("inspect_steward._tend.rulings.requeue_sample", requeue)

    failed_at = "2026-08-31T01:00:00Z"
    reruled_at = "2026-08-31T02:00:00Z"
    fresh = replace(inst("s1"), ref="e1:s1:1:u-fresh", uuid="u-fresh")
    reruled = replace(
        over(inst("s1"), ts=reruled_at),
        failed_refs={fresh.ref: failed_at},
    )

    apply(workspace, census(fresh), anomalies=Anomalies(open=(reruled,)))

    assert calls == [("T1", "s1", 1)]


def test_a_failure_the_ruling_never_saw_is_not_rerun_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ruled population is the window's refs, never an attempt instant.

    A fresh failure inside the same still-running attempt predates nothing:
    it opens the next generation, awaiting its own ruling — requeuing it
    under the old one would be a re-run nobody authorized.
    """
    workspace = workspace_at(tmp_path)
    calls: list[str] = []

    def requeue(task_id: str, sample_id: str, epoch: int) -> RequeueView | Unavailable:
        calls.append(sample_id)
        return accepted()

    monkeypatch.setattr("inspect_steward._tend.rulings.requeue_sample", requeue)
    # the window absorbed s1 and was ruled; s2 errored after the ruling, in
    # the same attempt, so the census holds both
    ruled = Anomalies(open=(over(inst("s1")),))

    apply(workspace, census(inst("s1"), inst("s2")), anomalies=ruled)

    assert calls == ["s1"]
    (event,) = applications(workspace)
    assert [entry["id"] for entry in event["requeued"]] == ["s1"]


def test_one_decision_over_two_generations_is_applied_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a class-scoped ruling closes every non-terminal window of its class, so
    # two generations can stand RULED under one instant -- their refs merge
    # into one application, one journal event, no duplicate directives for
    # the task they share
    workspace = workspace_at(tmp_path)
    calls: list[str] = []

    def requeue(task_id: str, sample_id: str, epoch: int) -> RequeueView | Unavailable:
        calls.append(sample_id)
        return accepted()

    monkeypatch.setattr("inspect_steward._tend.rulings.requeue_sample", requeue)
    both = Anomalies(
        open=(
            over(inst("s1"), generation=1),
            over(inst("s2"), generation=2),
        )
    )

    apply(workspace, census(inst("s1"), inst("s2")), anomalies=both)

    assert sorted(calls) == ["s1", "s2"]
    (event,) = applications(workspace)
    assert len(event["requeued"]) == 2


# --- what reconcile is handed, and what the report reads -------------------


def test_rerun_ruled_maps_evidence_tasks_to_the_newest_instant() -> None:
    older = window(ts="2026-08-30T20:00:00Z", tasks=("a", "b"))
    newer = window(ts=RULED_AT, tasks=("b",), generation=2)

    assert rerun_ruled(Anomalies(open=(older, newer))) == {
        "a": "2026-08-30T20:00:00Z",
        "b": RULED_AT,
    }


def test_dispositions_count_only_the_current_attempt() -> None:
    # the split sits beside the errored cell, which counts the current log's
    # samples -- a superseded attempt's instance would make the two disagree
    superseded = Instance(
        class_key=CLASS,
        ref="e0:s9:1:u-s9",
        task=IDENT,
        location="logs/old.eval",
        attempt_created=CREATED,
        eval_id="e0",
        sample_id="s9",
        epoch=1,
        uuid="u-s9",
    )
    ruled = Anomalies(
        open=(),
        settled=(
            over(
                inst("s1"),
                inst("s2"),
                superseded,
                state=AnomalyState.ACCEPTED,
                disposition=Disposition.EXCLUDE,
            ),
        ),
    )
    fold = dispositions(
        census(inst("s1"), inst("s2"), superseded),
        ruled,
        {IDENT: "logs/a.eval"},
    )

    assert fold.by_task == {IDENT: {"excluded": 2}}
    assert (fold.excluded, fold.zeroed) == (2, 0)


def test_a_covered_rerun_failure_takes_the_rerulings_bucket() -> None:
    """Re-ruling after a failed re-run classifies the failure it covers.

    The failure's ref lives in `failed_refs`, never `refs` — mapped off `refs` alone it read undecided, kept `excluded` at 0, and wrote the wrong scoring denominator. Coverage is instant-aware in both directions: under the ruling that merely authorized the re-run, the same failure still awaits a decision.
    """
    failed_at = "2026-08-31T01:00:00Z"
    reruled_at = "2026-08-31T02:00:00Z"
    fresh = replace(inst("s1"), ref="e1:s1:1:u-fresh", uuid="u-fresh")

    awaiting = replace(over(inst("s1")), failed_refs={fresh.ref: failed_at})
    held = dispositions(
        census(inst("s1"), fresh), Anomalies(open=(awaiting,)), {IDENT: "logs/a.eval"}
    )
    assert held.by_task == {IDENT: {"rerunning": 1, "undecided": 1}}

    excluded = replace(
        over(
            inst("s1"),
            state=AnomalyState.ACCEPTED,
            disposition=Disposition.EXCLUDE,
            ts=reruled_at,
        ),
        failed_refs={fresh.ref: failed_at},
    )
    ruled = dispositions(
        census(inst("s1"), fresh),
        Anomalies(open=(), settled=(excluded,)),
        {IDENT: "logs/a.eval"},
    )
    assert ruled.by_task == {IDENT: {"excluded": 2}}
    assert ruled.excluded == 2


def test_the_report_splits_the_errored_cell_and_qualifies_the_score(
    tmp_path: Path,
) -> None:
    # the qualification beside the number, never a recomputed number: the
    # headline still comes off the log verbatim, and these two lines say what
    # population it describes
    workspace = erroring(tmp_path)
    turn(workspace)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")

    document = status_markdown(turn(workspace))

    assert "2 (2 excluded)" in document
    assert "Scores are over 2 of 4 samples (2 excluded)." in document


def test_a_decision_survives_the_next_generation_opening_beside_it() -> None:
    # per instance, the window that absorbed it decides: an excluded first
    # generation must not read as undecided because a fresh second one opened
    # -- that would move the scoring denominator back on samples already ruled
    generations = Anomalies(
        open=(over(inst("s2"), state=AnomalyState.OPEN, generation=2),),
        settled=(
            over(
                inst("s1"),
                state=AnomalyState.ACCEPTED,
                disposition=Disposition.EXCLUDE,
                generation=1,
            ),
        ),
    )

    fold = dispositions(
        census(inst("s1"), inst("s2")), generations, {IDENT: "logs/a.eval"}
    )

    assert fold.by_task == {IDENT: {"excluded": 1, "undecided": 1}}
    assert (fold.excluded, fold.zeroed) == (1, 0)


def test_an_unruled_class_is_undecided_and_a_ruled_rerun_is_rerunning() -> None:
    undecided = dispositions(census(inst("s1")), Anomalies(), {IDENT: "logs/a.eval"})
    rerunning = dispositions(
        census(inst("s1")),
        Anomalies(open=(over(inst("s1")),)),
        {IDENT: "logs/a.eval"},
    )

    assert undecided.by_task == {IDENT: {"undecided": 1}}
    assert rerunning.by_task == {IDENT: {"rerunning": 1}}
    assert undecided.excluded == rerunning.excluded == 0
