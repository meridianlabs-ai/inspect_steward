"""The applied-rulings fold: which targets of which rulings have already been acted on.

`_tend.rulings` applies a `rerun` ruling — a warm requeue into a running task, an invalidation of a landed log — and journals what it did as an `action` event (`action="ruling_applied"`). This module folds those events back into per-target memory, keyed by `(class, ruling ts)`, for the two consumers that need it: the applier itself, which retries exactly what has not been applied, and the anomaly fold's pass check, which needs a witness that a warm re-run actually happened before it can call a quiet, completed task recovered.

**Per-target records, and "fully applied" is never stored.** Whether a ruling stands fully applied is derived each turn as *the applicable census minus this fold is empty* — recomputed from data both sides already hold. A stored boolean would be a false witness the first time a turn journaled it while some targets 409'd, and the warm pass branch would then resolve `reran_passed` over samples that never re-ran. Partial applications therefore retry exactly the remainder, and nothing can go stale.

**Keyed by the ruling's ts**, so a fresh ruling — a new instant — owes a fresh application. That is what makes re-ruling after a failed re-run re-apply automatically: the applicable set is recomputed against the new instant and the fold holds no records `for` it.

The `deferred` list an application event may carry is provenance, never memory: deferral is recomputed next turn as census-minus-applied, and reading it back would turn a display field into state.
"""

from dataclasses import dataclass, field
from typing import Any, cast

from .._workspace.journal import ACTION, JournalEvent

RULING_APPLIED = "ruling_applied"
"""The `action` value of an application event. Written by `_tend.rulings`, folded here, rendered by `_tend.history`."""


@dataclass(frozen=True)
class Application:
    """One application event: what one turn did about one class's ruling."""

    ruling_ts: str
    """The ruling this discharges — the event's `for` field, matching `Ruling.ts`."""

    ts: str
    """When the application was journaled — the witness instant."""

    requeued: frozenset[tuple[str, str, int]] = frozenset()
    """`(task, sample_id, epoch)` warm-requeued into a running task — accepted, or already converged (`changed: false`)."""

    invalidated: frozenset[str] = frozenset()
    """Sample uuids written invalid into a landed log (or found already invalid — the crash-recovery record)."""

    accepted: frozenset[str] = frozenset()
    """Tasks an `accept` ruling was carried out on — the log marked `success`, or found needing no mark.

    Task-grained where the two above are sample-grained, because that is the grain of the act: an acceptance decides about a *log*, and the four outcomes that need no write (already `success`, still being written, no log at all, a superseded attempt already answered) are recorded here exactly like the one that does. Without them a `limit:` acceptance over an already-successful log would be re-examined every turn for the life of the run.
    """

    tasks: frozenset[str] = frozenset()
    """Every task this event covered — requeued, invalidated, accepted, or converged with nothing left to do."""


@dataclass(frozen=True)
class Applied:
    """Every application the journal holds, grouped by class."""

    by_class: dict[str, tuple[Application, ...]] = field(
        default_factory=dict[str, tuple["Application", ...]]
    )
    """Applications per class key, in file order."""

    def warm_targets(
        self, class_key: str, ruling_ts: str
    ) -> frozenset[tuple[str, str, int]]:
        """The samples already warm-requeued for this ruling."""
        targets: set[tuple[str, str, int]] = set()
        for application in self.by_class.get(class_key, ()):
            if application.ruling_ts == ruling_ts:
                targets |= application.requeued
        return frozenset(targets)

    def invalidated_uuids(self, class_key: str, ruling_ts: str) -> frozenset[str]:
        """The sample uuids already invalidated for this ruling."""
        uuids: set[str] = set()
        for application in self.by_class.get(class_key, ()):
            if application.ruling_ts == ruling_ts:
                uuids |= application.invalidated
        return frozenset(uuids)

    def accepted_tasks(self, class_key: str, ruling_ts: str) -> frozenset[str]:
        """The tasks this acceptance has already been carried out on.

        The applier's remainder comes off this: an `accept` acts on logs, so what is left is the window's evidence tasks minus these. The log's own status is the second signal and answers a different question — see `_tend.rulings._accept`.
        """
        tasks: set[str] = set()
        for application in self.by_class.get(class_key, ()):
            if application.ruling_ts == ruling_ts:
                tasks |= application.accepted
        return frozenset(tasks)

    def witness(self, class_key: str, ruling_ts: str, task: str) -> str | None:
        """When this ruling was last applied to this task, or `None` while it never was.

        The warm pass branch's evidence: a task that completed without a new attempt recovered only if something actually re-ran its ruled samples — or verifiably had nothing left to re-run — and this is the record that says so.
        """
        stamps = [
            application.ts
            for application in self.by_class.get(class_key, ())
            if application.ruling_ts == ruling_ts and task in application.tasks
        ]
        return max(stamps) if stamps else None


def read_applied(events: list[JournalEvent]) -> Applied:
    """Fold the journal down to what has been applied.

    Args:
        events: The journal, in file order.

    Returns:
        Every application, per class. Payloads this version cannot read are data, not damage — an event missing its class or ruling ts is skipped, never raised on.
    """
    by_class: dict[str, list[Application]] = {}
    for event in events:
        if event.type != ACTION or event.payload.get("action") != RULING_APPLIED:
            continue
        class_key = event.payload.get("class")
        ruling_ts = event.payload.get("for")
        if (
            not isinstance(class_key, str)
            or not class_key
            or not isinstance(ruling_ts, str)
            or not ruling_ts
        ):
            continue
        requeued = _requeued(event.payload.get("requeued"))
        invalidated, invalidated_tasks = _invalidated(event.payload.get("invalidated"))
        accepted = _accepted(event.payload.get("accepted"))
        converged = _strings(event.payload.get("converged"))
        by_class.setdefault(class_key, []).append(
            Application(
                ruling_ts=ruling_ts,
                ts=event.ts,
                requeued=requeued,
                invalidated=invalidated,
                accepted=accepted,
                tasks=frozenset(
                    {target[0] for target in requeued}
                    | invalidated_tasks
                    | accepted
                    | set(converged)
                ),
            )
        )
    return Applied(by_class={key: tuple(listed) for key, listed in by_class.items()})


def _requeued(value: object) -> frozenset[tuple[str, str, int]]:
    targets: set[tuple[str, str, int]] = set()
    for entry in cast(list[object], value) if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        record = cast(dict[str, Any], entry)
        task, sample_id, epoch = (
            record.get("task"),
            record.get("id"),
            record.get("epoch"),
        )
        if (
            isinstance(task, str)
            and task
            and isinstance(sample_id, str)
            and isinstance(epoch, int)
            and not isinstance(epoch, bool)
        ):
            targets.add((task, sample_id, epoch))
    return frozenset(targets)


def _invalidated(value: object) -> tuple[frozenset[str], set[str]]:
    uuids: set[str] = set()
    tasks: set[str] = set()
    for entry in cast(list[object], value) if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        record = cast(dict[str, Any], entry)
        task = record.get("task")
        if isinstance(task, str) and task:
            tasks.add(task)
        uuids.update(_strings(record.get("uuids")))
    return frozenset(uuids), tasks


def _accepted(value: object) -> frozenset[str]:
    tasks: set[str] = set()
    for entry in cast(list[object], value) if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        task = cast(dict[str, Any], entry).get("task")
        if isinstance(task, str) and task:
            tasks.add(task)
    return frozenset(tasks)


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        entry for entry in cast(list[object], value) if isinstance(entry, str) and entry
    ]


__all__ = [
    "RULING_APPLIED",
    "Application",
    "Applied",
    "read_applied",
]
