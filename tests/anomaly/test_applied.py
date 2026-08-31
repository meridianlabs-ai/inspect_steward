"""The applied-rulings fold: per-target memory, keyed by the ruling it discharges.

The claims worth defending: records key on `(class, ruling ts)` so a fresh ruling owes a fresh application; the accessors answer per target and per task rather than as a boolean (a stored "fully applied" would be a false witness after a partial application); `deferred` is provenance and never read back; and payloads this version cannot read are data, not damage.

Everything here is synthesized events — no files, no logs, no clock.
"""

from typing import Any

from inspect_steward._anomaly.applied import RULING_APPLIED, read_applied
from inspect_steward._workspace import ACTION, JournalEvent

T0, T1, T2, T3 = (f"2026-08-30T1{n}:00:00Z" for n in range(4))

CLASS = "error:TimeoutError@openai/_client.py:post"


def applied(
    ts: str,
    *,
    cls: str = CLASS,
    ruled: str = T0,
    **fields: Any,
) -> JournalEvent:
    payload: dict[str, Any] = {
        "ts": ts,
        "type": ACTION,
        "action": RULING_APPLIED,
        "class": cls,
        "for": ruled,
        **fields,
    }
    return JournalEvent.model_validate(payload)


def test_the_fold_answers_per_target_and_per_task() -> None:
    events = [
        applied(
            T1,
            requeued=[{"task": "a", "id": "s1", "epoch": 1}],
            invalidated=[
                {"task": "b", "location": "l", "eval_id": "e", "uuids": ["u1", "u2"]}
            ],
            converged=["c"],
        )
    ]

    fold = read_applied(events)

    assert fold.warm_targets(CLASS, T0) == {("a", "s1", 1)}
    assert fold.invalidated_uuids(CLASS, T0) == {"u1", "u2"}
    # every task the event covered is witnessed, converged included
    for task in ("a", "b", "c"):
        assert fold.witness(CLASS, T0, task) == T1
    assert fold.witness(CLASS, T0, "d") is None


def test_a_partial_application_accumulates_rather_than_replacing() -> None:
    # two turns each landed part of the same ruling -- the 409-then-retry
    # shape -- and the memory is their union, never the last word
    events = [
        applied(T1, requeued=[{"task": "a", "id": "s1", "epoch": 1}]),
        applied(T2, requeued=[{"task": "a", "id": "s2", "epoch": 1}]),
    ]

    fold = read_applied(events)

    assert fold.warm_targets(CLASS, T0) == {("a", "s1", 1), ("a", "s2", 1)}
    # the witness is the newest stamp covering the task
    assert fold.witness(CLASS, T0, "a") == T2


def test_a_fresh_ruling_owes_a_fresh_application() -> None:
    # keyed by the ruling's ts: what was applied for T0 says nothing about a
    # re-ruling at T2, which is what makes re-apply-after-re-ruling automatic
    events = [applied(T1, ruled=T0, requeued=[{"task": "a", "id": "s1", "epoch": 1}])]

    fold = read_applied(events)

    assert fold.warm_targets(CLASS, T2) == frozenset()
    assert fold.invalidated_uuids(CLASS, T2) == frozenset()
    assert fold.witness(CLASS, T2, "a") is None


def test_classes_do_not_share_memory() -> None:
    other = "error:ReadTimeout@httpx/_client.py:send"
    events = [applied(T1, requeued=[{"task": "a", "id": "s1", "epoch": 1}])]

    assert read_applied(events).warm_targets(other, T0) == frozenset()


def test_deferred_is_provenance_and_never_memory() -> None:
    # a deferred-only event carries no targets, so the fold holds nothing for
    # it -- deferral is recomputed each turn as census-minus-applied
    events = [
        applied(T1, deferred=[{"task": "a", "id": "s1", "epoch": 1, "why": "busy"}])
    ]

    fold = read_applied(events)

    assert fold.warm_targets(CLASS, T0) == frozenset()
    assert fold.witness(CLASS, T0, "a") is None


def test_a_payload_this_version_cannot_read_is_data_not_damage() -> None:
    unreadable = [
        # no class, no ruling ts -- skipped whole
        applied(T1, cls="", ruled=""),
        # entries that are not records -- skipped per entry
        applied(
            T2,
            requeued=["s1", {"task": "a", "id": "s1", "epoch": True}],
            invalidated=[{"task": "b", "uuids": "u1"}],
            converged=[3],
        ),
    ]

    fold = read_applied(unreadable)

    assert fold.warm_targets(CLASS, T0) == frozenset()
    assert fold.invalidated_uuids(CLASS, T0) == frozenset()
    # the malformed-entries event still witnessed task b: its record named it
    assert fold.witness(CLASS, T0, "b") == T2
