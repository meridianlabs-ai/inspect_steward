"""The anomaly fold, and the delta the absorb step owes the journal.

The claims worth defending: replaying the journal reproduces state exactly (crash recovery is the ordinary path); absorbing the same census twice journals nothing (idempotence, which is what makes cache loss free); a ruling closes a window and recurrence opens the next generation with precedent attached; a re-run's failure lands on the ruled window as a resolution rather than reading as news; and tasks heal mechanically where samples never do.

Everything here is synthesized events and instances — no files, no logs, no clock.
"""

from dataclasses import replace
from typing import Any

from inspect_steward._anomaly.applied import Application, Applied
from inspect_steward._anomaly.fold import (
    SAMPLE_CAP,
    Pending,
    TaskHealth,
    absorb,
    as_events,
    covered_refs,
    read_anomalies,
)
from inspect_steward._anomaly.model import (
    Anomalies,
    AnomalyState,
    Disposition,
    Outcome,
)
from inspect_steward._evalset.instances import Instance, InstanceBatch
from inspect_steward._workspace import (
    INSTANCE,
    INVESTIGATING,
    OPENED,
    PROPOSAL,
    RESOLUTION,
    RULING,
    JournalEvent,
)

T0, T1, T2, T3, T4 = (f"2026-08-30T1{n}:00:00Z" for n in range(5))

CLASS = "error:TimeoutError@openai/_client.py:post"


def ev(type: str, ts: str, **fields: Any) -> JournalEvent:
    return JournalEvent.model_validate({"ts": ts, "type": type, **fields})


def opened(cls: str = CLASS, *, ts: str = T0, kind: str = "error") -> JournalEvent:
    return ev(OPENED, ts, **{"class": cls, "kind": kind})


def ref(sample_id: str, *, eval_id: str = "ev1", epoch: int = 1) -> str:
    """The content-derived ref `inst()` would build for this sample."""
    return f"{eval_id}:{sample_id}:{epoch}:u-{eval_id}-{sample_id}-{epoch}"


def instance(
    cls: str = CLASS,
    *,
    ts: str = T0,
    count: int = 1,
    refs: list[str] | None = None,
    **fields: Any,
) -> JournalEvent:
    listed = refs if refs is not None else [ref(f"s{n + 1}") for n in range(count)]
    return ev(
        INSTANCE,
        ts,
        **{"class": cls, "count": count, "refs": listed},
        **fields,
    )


def ruling(
    cls: str = CLASS,
    *,
    ts: str = T1,
    disposition: str = "rerun",
    reason: str = "provider outage, retry",
    by: str = "kaia",
    **fields: Any,
) -> JournalEvent:
    return ev(
        RULING,
        ts,
        **{"class": cls, "disposition": disposition, "reason": reason, "by": by},
        **fields,
    )


def inst(
    cls: str = CLASS,
    *,
    eval_id: str = "ev1",
    sample_id: str = "s1",
    epoch: int = 1,
    task: str = "taskA",
    created: str = T0,
    message: str = "TimeoutError('too slow')",
) -> Instance:
    uuid = f"u-{eval_id}-{sample_id}-{epoch}"
    built = (
        f"{task}@{eval_id}"
        if cls.startswith("task")
        else ref(sample_id, eval_id=eval_id, epoch=epoch)
    )
    return Instance(
        class_key=cls,
        ref=built,
        task=task,
        location=f"logs/{eval_id}.eval",
        message=message,
        attempt_created=created,
        eval_id=eval_id,
        sample_id=sample_id,
        epoch=epoch,
        uuid=uuid,
    )


def batch(*instances: Instance, substrate: bool = False) -> InstanceBatch:
    one = instances[0]
    return InstanceBatch(
        class_key=one.class_key,
        kind=one.kind,
        substrate=substrate,
        instances=instances,
    )


def folded(events: list[JournalEvent], pending: list[Pending]) -> Anomalies:
    """State-if-executed: the one transition function applied to both."""
    return read_anomalies(events + as_events(pending, T4))


class TestFoldTransitions:
    def test_opened_and_instance_make_one_open_window(self) -> None:
        events = [
            opened(),
            instance(
                count=3,
                samples=["s1:1", "s2:1"],
                tasks=["taskA"],
                logs=["logs/ev1.eval"],
                exemplar="TimeoutError('too slow')",
            ),
        ]

        state = read_anomalies(events)

        assert len(state.open) == 1
        anomaly = state.open[0]
        assert anomaly.class_key == CLASS
        assert anomaly.state is AnomalyState.OPEN
        assert anomaly.kind == "error"
        assert anomaly.generation == 1
        assert anomaly.evidence.count == 3
        assert anomaly.evidence.samples == ("s1:1", "s2:1")
        assert anomaly.evidence.tasks == ("taskA",)
        assert anomaly.evidence.exemplar == "TimeoutError('too slow')"
        assert state.absorbed_refs[CLASS] == frozenset(
            {ref("s1"), ref("s2"), ref("s3")}
        )

    def test_an_instance_with_no_opened_line_opens_defensively(self) -> None:
        # an `opened` lost to a torn line must not lose the batch
        state = read_anomalies([instance(count=2)])

        assert len(state.open) == 1
        assert state.open[0].evidence.count == 2

    def test_a_replayed_instance_event_changes_nothing(self) -> None:
        # a verb persisting a window it is about to decide can land the same
        # batch a concurrent tend also lands; the second copy's refs are all
        # absorbed, so it must be a no-op
        event = instance(count=2)

        state = read_anomalies([opened(), event, event])

        assert len(state.open) == 1
        assert state.open[0].evidence.count == 2

    def test_an_opened_with_no_instances_is_invisible(self) -> None:
        # a torn write, or a stale `opened` replayed after a ruling closed the
        # window: not an empty question -- if instances exist they were never
        # absorbed, so a later turn re-emits them and the window surfaces
        assert read_anomalies([opened()]).open == ()

    def test_instances_accumulate_and_the_ledger_unions_refs(self) -> None:
        events = [
            opened(),
            instance(ts=T0, count=3, exemplar="first"),
            instance(
                ts=T2,
                count=2,
                refs=[ref("s4"), ref("s1", eval_id="ev2")],
                exemplar="second",
            ),
        ]

        state = read_anomalies(events)

        anomaly = state.open[0]
        assert anomaly.evidence.count == 5
        assert anomaly.evidence.first_ts == T0
        assert anomaly.evidence.last_ts == T2
        # the exemplar is one message, first wins
        assert anomaly.evidence.exemplar == "first"
        assert state.absorbed_refs[CLASS] == frozenset(
            {ref("s1"), ref("s2"), ref("s3"), ref("s4"), ref("s1", eval_id="ev2")}
        )

    def test_later_evidence_promotes_a_window_to_substrate(self) -> None:
        # the flag only ratchets on: a class opened by an unflagged message
        # must not stay rerun-proposable after ENOSPC shows up in it
        events = [
            opened(),
            instance(),
            instance(ts=T1, refs=[ref("s2")], substrate=True),
        ]

        state = read_anomalies(events)

        assert state.open[0].substrate is True

    def test_investigating_holds_the_class_with_its_note(self) -> None:
        events = [
            opened(),
            instance(),
            ev(
                INVESTIGATING,
                T1,
                **{"class": CLASS, "by": "agent", "note": "reading logs"},
            ),
        ]

        state = read_anomalies(events)

        assert state.open[0].state is AnomalyState.INVESTIGATING
        assert state.open[0].note == "reading logs"

    def test_a_proposal_covers_its_classes_and_is_live(self) -> None:
        other = "error:ValueError@evals/scorer.py:score"
        events = [
            opened(),
            instance(),
            opened(other),
            instance(other),
            ev(
                PROPOSAL,
                T1,
                id="prop-abcd1234",
                action="rerun",
                classes={CLASS: {"count": 1}, other: {"count": 1}},
                reason="both transient",
                by="agent",
            ),
        ]

        state = read_anomalies(events)

        assert all(a.state is AnomalyState.PROPOSED for a in state.open)
        assert all(a.proposal == "prop-abcd1234" for a in state.open)
        proposal = state.proposals["prop-abcd1234"]
        assert proposal.action is Disposition.RERUN
        assert set(proposal.classes) == {CLASS, other}
        assert proposal.evidence[CLASS].count == 1

    def test_a_partial_answer_keeps_the_remainder_proposed(self) -> None:
        other = "error:ValueError@evals/scorer.py:score"
        events = [
            opened(),
            instance(),
            opened(other),
            instance(other),
            ev(
                PROPOSAL,
                T1,
                id="prop-abcd1234",
                action="rerun",
                classes={CLASS: {"count": 1}, other: {"count": 1}},
            ),
            ruling(ts=T2, proposal="prop-abcd1234"),
        ]

        state = read_anomalies(events)

        ruled = next(a for a in state.open if a.class_key == CLASS)
        remaining = next(a for a in state.open if a.class_key == other)
        assert ruled.state is AnomalyState.RULED
        assert remaining.state is AnomalyState.PROPOSED
        # the proposal is still live: one covered class still awaits a ruling
        assert "prop-abcd1234" in state.proposals

    def test_dispositions_settle_where_the_doctrine_says(self) -> None:
        cases: list[tuple[str, AnomalyState]] = [
            ("rerun", AnomalyState.RULED),
            ("exclude", AnomalyState.ACCEPTED),
            ("zero", AnomalyState.ACCEPTED),
            ("score", AnomalyState.ACCEPTED),
            ("accept", AnomalyState.ACCEPTED),
            ("dismiss", AnomalyState.RESOLVED),
        ]
        for disposition, expected in cases:
            state = read_anomalies(
                [opened(), instance(), ruling(disposition=disposition)]
            )
            windows = state.open if expected is AnomalyState.RULED else state.settled
            assert len(windows) == 1, disposition
            assert windows[0].state is expected, disposition
            assert windows[0].ruling is not None

    def test_a_ruling_with_an_unknown_disposition_is_data_not_damage(self) -> None:
        state = read_anomalies([opened(), instance(), ruling(disposition="explode")])

        assert state.open[0].state is AnomalyState.OPEN
        assert state.open[0].ruling is None

    def test_recurrence_after_a_ruling_opens_the_next_generation(self) -> None:
        events = [
            opened(ts=T0),
            instance(ts=T0),
            ruling(ts=T1, disposition="dismiss", reason="one-off"),
            opened(ts=T2),
            instance(ts=T2, refs=[ref("s1", eval_id="ev2")]),
        ]

        state = read_anomalies(events)

        assert len(state.settled) == 1
        assert len(state.open) == 1
        recurred = state.open[0]
        assert recurred.generation == 2
        assert [p.reason for p in recurred.precedent] == ["one-off"]
        # the ledger spans windows: a closed window's instances are not news
        assert state.absorbed_refs[CLASS] == frozenset(
            {ref("s1"), ref("s1", eval_id="ev2")}
        )

    def test_a_ruling_answers_every_open_window_of_the_class(self) -> None:
        events = [
            opened(ts=T0),
            instance(ts=T0),
            ruling(ts=T1),  # rerun -> RULED, still open
            opened(ts=T2),
            instance(ts=T2, refs=[ref("s1", eval_id="ev2")]),
            ruling(ts=T3, disposition="dismiss", reason="noise"),
        ]

        state = read_anomalies(events)

        assert state.open == ()
        assert len(state.settled) == 2
        # the superseded rerun is precedent on the window it was ruled on
        first = next(a for a in state.settled if a.generation == 1)
        assert [p.disposition for p in first.precedent] == [Disposition.RERUN]

    def test_reran_passed_resolves_and_reran_failed_stays_ruled(self) -> None:
        base = [opened(), instance(), ruling(ts=T1)]

        passed = read_anomalies(
            base + [ev(RESOLUTION, T2, **{"class": CLASS, "outcome": "reran_passed"})]
        )
        failed = read_anomalies(
            base
            + [
                ev(
                    RESOLUTION,
                    T2,
                    **{
                        "class": CLASS,
                        "outcome": "reran_failed",
                        "refs": [ref("s1", eval_id="ev9")],
                    },
                )
            ]
        )

        assert passed.open == ()
        assert passed.settled[0].state is AnomalyState.RESOLVED
        still = failed.open[0]
        assert still.state is AnomalyState.RULED
        assert still.failed_resolutions == 1
        assert still.resolution is not None
        assert still.resolution.outcome is Outcome.RERAN_FAILED
        # what the re-run consumed is absorbed, not news
        assert ref("s1", eval_id="ev9") in failed.absorbed_refs[CLASS]

    def test_a_resolution_lands_on_the_ruled_window_not_the_recurrence(self) -> None:
        events = [
            opened(ts=T0),
            instance(ts=T0),
            ruling(ts=T1),
            opened(ts=T2),
            instance(ts=T2, refs=[ref("s1", eval_id="ev2")]),
            ev(RESOLUTION, T3, **{"class": CLASS, "outcome": "reran_passed"}),
        ]

        state = read_anomalies(events)

        resolved = next(a for a in state.settled if a.class_key == CLASS)
        assert resolved.generation == 1
        assert state.open[0].generation == 2
        assert state.open[0].state is AnomalyState.OPEN


class TestAbsorb:
    def test_a_fresh_census_opens_and_absorbs_in_one_turn(self) -> None:
        census = [
            batch(
                inst(sample_id="s1"),
                inst(sample_id="s2"),
                inst(sample_id="s2", epoch=2),
            )
        ]

        pending = absorb(read_anomalies([]), census, {})

        assert [p.type for p in pending] == [OPENED, INSTANCE]
        assert pending[1].fields["count"] == 3
        assert sorted(pending[1].fields["refs"]) == sorted(
            [ref("s1"), ref("s2"), ref("s2", epoch=2)]
        )
        assert pending[1].fields["tasks"] == ["taskA"]
        state = folded([], pending)
        assert state.open[0].evidence.count == 3

    def test_absorbing_the_same_census_twice_journals_nothing(self) -> None:
        census = [batch(inst(sample_id="s1"), inst(sample_id="s2"))]
        first = absorb(read_anomalies([]), census, {})

        second = absorb(folded([], first), census, {})

        assert second == []

    def test_a_growing_eval_journals_only_the_excess(self) -> None:
        census = [batch(*(inst(sample_id=f"s{n}") for n in range(5)))]
        state = read_anomalies([opened(), instance(count=3)])

        pending = absorb(state, census, {})

        assert [p.type for p in pending] == [INSTANCE]
        assert pending[0].fields["count"] == 2
        assert sorted(pending[0].fields["refs"]) == sorted([ref("s0"), ref("s4")])
        assert folded([], []).open == ()  # sanity: helpers fold cleanly

    def test_task_attempts_dedupe_by_ref(self) -> None:
        cls = "task:no-log-exit:ModuleNotFoundError@work/evalset.py:<module>"
        census = [batch(inst(cls, eval_id="w1"), inst(cls, eval_id="w2", task="taskB"))]
        first = absorb(read_anomalies([]), census, {})

        second = absorb(folded([], first), census, {})

        assert [p.type for p in first] == [OPENED, INSTANCE]
        assert sorted(first[1].fields["refs"]) == ["taskA@w1", "taskB@w2"]
        assert second == []

    def test_sample_evidence_is_capped_but_counts_are_not(self) -> None:
        census = [batch(*(inst(sample_id=f"s{n:03d}") for n in range(50)))]

        pending = absorb(read_anomalies([]), census, {})

        assert pending[1].fields["count"] == 50
        assert len(pending[1].fields["samples"]) == SAMPLE_CAP

    def test_substrate_travels_from_census_to_window(self) -> None:
        # testing.md's expired-credentials night: the population is flagged as
        # the machinery under the run, so no re-run gets proposed off it
        cls = "error:NoCredentialsError@aiobotocore/credentials.py:load"
        census = [batch(inst(cls), substrate=True)]

        state = folded([], absorb(read_anomalies([]), census, {}))

        assert state.open[0].substrate is True


class TestRoutingAfterARuling:
    def test_the_ruled_samples_failing_in_a_newer_attempt_is_reran_failed(
        self,
    ) -> None:
        events = [
            opened(ts=T0),
            instance(ts=T0, count=2),
            ruling(ts=T1),
        ]
        # the re-run attempt (created after the ruling) fails the same sample,
        # while the old attempt's instances are still in the census
        census = [
            batch(
                inst(sample_id="s1", created=T0),
                inst(sample_id="s2", created=T0),
                inst(eval_id="ev2", sample_id="s1", created=T2),
            )
        ]

        pending = absorb(read_anomalies(events), census, {})

        assert [p.type for p in pending] == [RESOLUTION]
        assert pending[0].fields["outcome"] == "reran_failed"
        assert pending[0].fields["refs"] == [ref("s1", eval_id="ev2")]
        state = folded(events, pending)
        assert state.open[0].state is AnomalyState.RULED
        assert state.open[0].failed_resolutions == 1

    def test_a_new_sample_failing_the_same_way_opens_the_next_generation(
        self,
    ) -> None:
        events = [
            opened(ts=T0),
            instance(ts=T0, count=1),
            ruling(ts=T1),
        ]
        census = [
            batch(
                inst(sample_id="s1", created=T0),
                inst(eval_id="ev2", sample_id="s9", created=T2),
            )
        ]

        pending = absorb(read_anomalies(events), census, {})

        assert [p.type for p in pending] == [OPENED, INSTANCE]
        state = folded(events, pending)
        generations = {a.generation: a for a in state.open}
        assert generations[1].state is AnomalyState.RULED
        assert generations[2].state is AnomalyState.OPEN
        assert [p.by for p in generations[2].precedent] == ["kaia"]

    def test_a_different_sample_failing_after_a_ruling_is_news(self) -> None:
        # a requeue's ordinary shape: same eval, same count, different sample.
        # a count ledger slices this away as already-seen; the ref diff sees it
        events = [
            opened(ts=T0),
            instance(ts=T0),
            ruling(ts=T1, disposition="dismiss", reason="one-off"),
        ]
        census = [batch(inst(sample_id="s2", created=T0))]

        pending = absorb(read_anomalies(events), census, {})

        assert [p.type for p in pending] == [OPENED, INSTANCE]
        state = folded(events, pending)
        assert state.open[0].generation == 2

    def test_a_same_id_in_a_different_task_is_not_the_reruns_failure(self) -> None:
        # sample ids repeat across tasks, so membership carries the task: a
        # fresh failure in task B must not read as task A's re-run failing
        events = [opened(ts=T0), instance(ts=T0, tasks=["taskA"]), ruling(ts=T1)]
        census = [
            batch(
                inst(sample_id="s1", created=T0),
                inst(eval_id="ev2", sample_id="s1", task="taskB", created=T2),
            )
        ]

        pending = absorb(read_anomalies(events), census, {})

        assert [p.type for p in pending] == [OPENED, INSTANCE]
        state = folded(events, pending)
        assert state.open[0].failed_resolutions == 0
        assert {a.generation for a in state.open} == {1, 2}

    def test_a_ruled_task_failing_again_is_reran_failed_without_the_census(
        self,
    ) -> None:
        # the first departure's instance left the census when its worker was
        # reaped; membership for task classes is the window's evidence
        cls = "task:no-log-exit:ModuleNotFoundError@work/evalset.py:<module>"
        events = [
            ev(OPENED, T0, **{"class": cls, "kind": "task"}),
            ev(
                INSTANCE,
                T0,
                **{"class": cls, "count": 1, "refs": ["taskA@w1"], "tasks": ["taskA"]},
            ),
            ruling(cls, ts=T1),
        ]
        census = [batch(inst(cls, eval_id="w2", task="taskA", created=T2))]

        pending = absorb(read_anomalies(events), census, {})

        assert [p.fields.get("outcome") for p in pending] == ["reran_failed"]
        assert pending[0].fields["refs"] == ["taskA@w2"]

    def test_a_score_reruns_failure_routes_by_the_task_not_the_census(self) -> None:
        # a score ref names its attempt, and the invalidated attempt has left
        # the census (uniform-zero reads the current attempt only) -- so
        # membership reads off the window's evidence tasks, exactly as task
        # windows do; the class key is already task-scoped. Routed by census
        # refs, another all-zero result opened generation two while the ruled
        # window sat RULED in silence, and the failed-rerun review never fired
        cls = "score:zero:probe:abcd1234"
        events = [
            opened(cls, ts=T0, kind="score"),
            ev(
                INSTANCE,
                T0,
                **{
                    "class": cls,
                    "count": 1,
                    "refs": ["taskA@ev1"],
                    "tasks": ["taskA"],
                },
            ),
            ruling(cls, ts=T1),
        ]
        again = Instance(
            class_key=cls,
            ref="taskA@ev2",
            task="taskA",
            location="logs/ev2.eval",
            message="headline is 0.0 again",
            attempt_created=T2,
            eval_id="ev2",
        )

        pending = absorb(read_anomalies(events), [batch(again)], {})

        assert [p.fields.get("outcome") for p in pending] == ["reran_failed"]
        state = folded(events, pending)
        assert [window.generation for window in state.open] == [1]
        assert state.open[0].failed_resolutions == 1

    def test_a_reran_failure_is_not_news_on_the_following_turn(self) -> None:
        events = [
            opened(ts=T0),
            instance(ts=T0, count=1),
            ruling(ts=T1),
        ]
        census = [
            batch(
                inst(sample_id="s1", created=T0),
                inst(eval_id="ev2", sample_id="s1", created=T2),
            )
        ]
        first = absorb(read_anomalies(events), census, {})

        second = absorb(folded(events, first), census, {})

        assert [p.type for p in first] == [RESOLUTION]
        assert second == []


class TestResolutionDetection:
    def test_a_ruled_class_passes_when_its_tasks_recover_after_the_ruling(
        self,
    ) -> None:
        events = [
            opened(ts=T0),
            instance(ts=T0, tasks=["taskA"]),
            ruling(ts=T1),
        ]
        health = {"taskA": TaskHealth(complete=True, settled=T2)}

        pending = absorb(read_anomalies(events), [], health)

        assert [p.type for p in pending] == [RESOLUTION]
        assert pending[0].fields["outcome"] == "reran_passed"
        state = folded(events, pending)
        assert state.open == ()
        assert state.settled[0].state is AnomalyState.RESOLVED

    def test_a_recovery_older_than_the_ruling_does_not_pass_it(self) -> None:
        # the sample-kind re-run must land as a new attempt; a COMPLETE task
        # whose current attempt predates the ruling proves nothing about it
        events = [opened(ts=T0), instance(ts=T0, tasks=["taskA"]), ruling(ts=T2)]
        health = {"taskA": TaskHealth(complete=True, settled=T1)}

        assert absorb(read_anomalies(events), [], health) == []

    def test_new_instances_hold_the_pass_back(self) -> None:
        events = [
            opened(ts=T0),
            instance(ts=T0, count=1, tasks=["taskA"]),
            ruling(ts=T1),
        ]
        health = {"taskA": TaskHealth(complete=True, settled=T2)}
        census = [
            batch(
                inst(sample_id="s1", created=T0),
                inst(eval_id="ev2", sample_id="s1", created=T2),
            )
        ]

        pending = absorb(read_anomalies(events), census, health)

        # the re-run failed; recovered-looking tasks do not make it a pass
        assert [p.fields.get("outcome") for p in pending] == ["reran_failed"]

    def test_a_failed_rerun_does_not_pass_on_the_next_tend(self) -> None:
        # after `reran_failed` the failed instances are absorbed, so the class
        # goes quiet on an identical census -- but quiet is not recovered: the
        # window stays RULED for an operator while the census holds post-ruling
        # failures
        events = [
            opened(ts=T0),
            instance(ts=T0, count=1, tasks=["taskA"]),
            ruling(ts=T1),
        ]
        census = [
            batch(
                inst(sample_id="s1", created=T0),
                inst(eval_id="ev2", sample_id="s1", created=T2),
            )
        ]
        health = {"taskA": TaskHealth(complete=True, settled=T2)}
        first = absorb(read_anomalies(events), census, health)
        after = folded(events, first)

        second = absorb(after, census, health)

        assert [p.fields.get("outcome") for p in first] == ["reran_failed"]
        assert second == []
        assert after.open[0].state is AnomalyState.RULED

    def test_a_fresh_ruling_re_arms_the_pass(self) -> None:
        # the way out of a failed re-run is an operator deciding again: the new
        # ruling's instant moves the boundary past the failed instances
        failed = ev(
            RESOLUTION,
            T2,
            **{
                "class": CLASS,
                "outcome": "reran_failed",
                "refs": [ref("s1", eval_id="ev2")],
            },
        )
        events = [
            opened(ts=T0),
            instance(ts=T0, count=1, tasks=["taskA"]),
            ruling(ts=T1),
            failed,
            ruling(ts=T3, reason="second try"),
        ]
        census = [
            batch(
                inst(sample_id="s1", created=T0),
                inst(eval_id="ev2", sample_id="s1", created=T2),
            )
        ]
        health = {"taskA": TaskHealth(complete=True, settled=T4)}

        pending = absorb(read_anomalies(events), census, health)

        assert [p.fields.get("outcome") for p in pending] == ["reran_passed"]

    def test_an_unruled_task_window_heals_mechanically(self) -> None:
        cls = "task:vanished"
        events = [
            ev(OPENED, T0, **{"class": cls, "kind": "task"}),
            ev(
                INSTANCE,
                T0,
                **{"class": cls, "count": 1, "refs": ["taskA@w1"], "tasks": ["taskA"]},
            ),
        ]
        health = {"taskA": TaskHealth(complete=True, settled=T0)}

        pending = absorb(read_anomalies(events), [], health)

        assert [p.type for p in pending] == [RESOLUTION]
        state = folded(events, pending)
        assert state.settled[0].state is AnomalyState.RESOLVED
        assert state.settled[0].resolution is not None
        assert "without a ruling" in state.settled[0].resolution.detail

    def test_an_unruled_sample_window_never_heals_itself(self) -> None:
        # the residue of an errored sample is in the data; the four-answer
        # question stands until an operator rules
        events = [opened(ts=T0), instance(ts=T0, tasks=["taskA"])]
        health = {"taskA": TaskHealth(complete=True, settled=T2)}

        assert absorb(read_anomalies(events), [], health) == []

    def test_an_incomplete_task_holds_everything_back(self) -> None:
        events = [opened(ts=T0), instance(ts=T0, tasks=["taskA"]), ruling(ts=T1)]
        health = {"taskA": TaskHealth(complete=False)}

        assert absorb(read_anomalies(events), [], health) == []


class TestWarmBoundaries:
    """A warm requeue never moves the attempt instant, so its outcomes ride the applied fold."""

    def applied(self, *applications: Application) -> Applied:
        return Applied(by_class={CLASS: applications})

    def test_a_warm_reruns_failure_routes_by_the_applied_record(self) -> None:
        # the re-run keeps (sample_id, epoch) and mints a fresh uuid inside
        # the same attempt, so `attempt_created` cannot route it -- the
        # requeue record is what says this is the re-run failing again
        events = [
            opened(ts=T0),
            instance(ts=T0, count=1, tasks=["taskA"]),
            ruling(ts=T1),
        ]
        warm = self.applied(
            Application(
                ruling_ts=T1,
                ts=T2,
                requeued=frozenset({("taskA", "s1", 1)}),
                tasks=frozenset({"taskA"}),
            )
        )
        fresh = replace(
            inst(sample_id="s1", created=T0), uuid="u-fresh", ref="ev1:s1:1:u-fresh"
        )
        census = [batch(inst(sample_id="s1", created=T0), fresh)]

        pending = absorb(read_anomalies(events), census, {}, warm)

        assert [p.fields.get("outcome") for p in pending] == ["reran_failed"]
        assert folded(events, pending).open[0].failed_resolutions == 1

    def test_one_tasks_warm_witness_does_not_excuse_the_other(self) -> None:
        # the boundary is per task: a class spanning two tasks needs each one
        # recovered on its own evidence
        events = [
            opened(ts=T0),
            instance(
                ts=T0,
                count=2,
                refs=[ref("s1"), ref("s2", eval_id="ev2")],
                tasks=["taskA", "taskB"],
            ),
            ruling(ts=T1),
        ]
        census = [
            batch(
                inst(sample_id="s1", task="taskA", created=T0),
                inst(eval_id="ev2", sample_id="s2", task="taskB", created=T0),
            )
        ]
        # both tasks complete, neither with a post-ruling attempt: only the
        # applied record can witness a recovery
        health = {
            "taskA": TaskHealth(complete=True, settled=T0),
            "taskB": TaskHealth(complete=True, settled=T0),
        }
        one = Application(
            ruling_ts=T1,
            ts=T2,
            requeued=frozenset({("taskA", "s1", 1)}),
            tasks=frozenset({"taskA"}),
        )
        other = Application(
            ruling_ts=T1,
            ts=T2,
            requeued=frozenset({("taskB", "s2", 1)}),
            tasks=frozenset({"taskB"}),
        )

        partial = absorb(read_anomalies(events), census, health, self.applied(one))
        whole = absorb(read_anomalies(events), census, health, self.applied(one, other))

        assert partial == []
        assert [p.fields.get("outcome") for p in whole] == ["reran_passed"]

    def test_the_newest_resolution_guards_the_replaced_record_trap(self) -> None:
        """A warm failure whose errored record a later manual re-run replaces.

        The census is then clean, the task quiet and COMPLETE with an attempt
        that postdates the ruling -- every other check reads recovered, and
        this refusal is the only one standing. A fresh ruling re-arms it.
        """
        events = [
            opened(ts=T0),
            instance(ts=T0, count=1, tasks=["taskA"]),
            ruling(ts=T1),
            ev(
                RESOLUTION,
                T2,
                **{
                    "class": CLASS,
                    "outcome": "reran_failed",
                    "detail": "1 failed again after the re-run",
                },
            ),
        ]
        health = {"taskA": TaskHealth(complete=True, settled=T4)}

        held = absorb(read_anomalies(events), [], health)
        rearmed = absorb(read_anomalies([*events, ruling(ts=T3)]), [], health)

        assert held == []
        assert [p.fields.get("outcome") for p in rearmed] == ["reran_passed"]

    def test_a_later_ruling_covers_the_failed_rerun_the_authorizing_one_never_does(
        self,
    ) -> None:
        """A `reran_failed` ref joins the window's coverage — per ruling instant.

        The regression: with membership read off `refs` alone, re-ruling after
        a failed re-run found nothing applicable, so the failed sample never
        re-ran and the window could pass with it still failed. The boundary
        matters in both directions — the ruling that authorized the re-run
        must never cover its own outcome (re-applying it would be an unruled
        re-run), while the later ruling, made with the failure on the record,
        covers exactly it.
        """
        failed = "ev1:s1:1:u-fresh"
        events = [
            opened(ts=T0),
            instance(ts=T0, count=1, tasks=["taskA"]),
            ruling(ts=T1),
            ev(
                RESOLUTION,
                T2,
                **{
                    "class": CLASS,
                    "outcome": "reran_failed",
                    "detail": "1 failed again after the re-run",
                    "refs": [failed],
                },
            ),
            ruling(ts=T3),
        ]

        (window,) = read_anomalies(events).open

        assert covered_refs(window, T1) == frozenset({ref("s1")})
        assert covered_refs(window, T3) == frozenset({ref("s1"), failed})

    def test_each_window_carries_exactly_what_it_absorbed(self) -> None:
        # the window's refs are the ruled population: what its ruling covers,
        # what the executor applies, what the report attributes -- so a ref
        # belongs to the generation that absorbed it and to no other
        events = [
            opened(ts=T0),
            instance(ts=T0, count=2),
            ruling(ts=T1),
            instance(ts=T2, refs=[ref("s9")]),
        ]

        state = read_anomalies(events)

        gen1, gen2 = state.open
        assert (gen1.generation, gen2.generation) == (1, 2)
        assert gen1.refs == frozenset({ref("s1"), ref("s2")})
        assert gen2.refs == frozenset({ref("s9")})
        assert state.absorbed_refs[CLASS] == gen1.refs | gen2.refs
