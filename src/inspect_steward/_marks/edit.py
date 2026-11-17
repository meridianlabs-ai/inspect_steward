"""Writing a marking ruling into a landed log.

`exclude` and `zero` used to be journal marks: the tend folded them into the by-task table and the *Scores are over N of M* note, and the log kept whatever score the sample had. Writing the decision into the file means every reader of the directory sees the result the operator signed rather than the wreckage under it.

**Inspect does the edit.** `exclude_samples` makes each score unscored with the reason `excluded`; `zero_samples` copies the verdicts of an empty attempt (the side run, `run.py`) with the reason `zeroed`. Both record a score edit with history and provenance and recompute the metrics. Steward adds the ruling to the provenance, finds a ruling already written after a crash, and defers a target whose sample has moved.

**Every function here is pure over an `EvalLog` read whole.** A score edit inserts a `ScoreEditEvent` into the sample's events, so a header-only read has nothing to edit and an `exclude_fields` read would write back a truncated transcript. `commit` is the one write, and it is the runner's to call under the workspace claim.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from inspect_ai.log import (
    EvalLog,
    EvalSample,
    ProvenanceData,
    exclude_samples,
    write_eval_log,
    zero_samples,
)

from .._anomaly.model import Anomaly, Disposition, Ruling
from .._evalset.instances import Instance

STEWARD = "steward"
"""The provenance metadata key under which the ruling travels — the shape the acceptance amendment records (`_tend.rulings._flip`)."""


@dataclass(frozen=True)
class Target:
    """One ruled sample, addressed the way a log can find it.

    The census instance's identity plus the log it is to be edited in — which is the task's **current** attempt, not necessarily the instance's own `location`: a scan row names the file the scanner read, and after a resume that is the superseded log while the sample, uuid intact, sits in the current one.
    """

    task: str
    location: str
    """The current attempt's log."""

    eval_id: str
    sample_id: str
    """As the census spells it, `str(sample.id)`. The edit itself takes the log's typed id."""

    epoch: int
    uuid: str

    @classmethod
    def of(cls, instance: Instance, location: str, eval_id: str) -> "Target":
        return cls(
            task=instance.task,
            location=location,
            eval_id=eval_id,
            sample_id=instance.sample_id,
            epoch=instance.epoch,
            uuid=instance.uuid,
        )

    @classmethod
    def from_record(cls, record: object) -> "Target | None":
        """A target as the runs record spells it, or `None` for a line this version cannot read."""
        if not isinstance(record, dict):
            return None
        fields = cast(dict[str, Any], record)
        task, location, eval_id, sample_id, epoch, uuid = (
            fields.get("task"),
            fields.get("location"),
            fields.get("eval_id"),
            fields.get("id"),
            fields.get("epoch"),
            fields.get("uuid"),
        )
        if not (
            isinstance(task, str)
            and task
            and isinstance(location, str)
            and location
            and isinstance(eval_id, str)
            and isinstance(sample_id, str)
            and isinstance(epoch, int)
            and not isinstance(epoch, bool)
            and isinstance(uuid, str)
            and uuid
        ):
            return None
        return cls(
            task=task,
            location=location,
            eval_id=eval_id,
            sample_id=sample_id,
            epoch=epoch,
            uuid=uuid,
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "location": self.location,
            "eval_id": self.eval_id,
            "id": self.sample_id,
            "epoch": self.epoch,
            "uuid": self.uuid,
        }


@dataclass
class Marked:
    """What one pass over one log did to its targets."""

    edited: list[Target] = field(default_factory=list[Target])
    """Targets whose scores this pass edited."""

    scores: set[str] = field(default_factory=set[str])
    """The score names touched, across every edited target."""

    found: list[Target] = field(default_factory=list[Target])
    """Targets already carrying this ruling — the crash-recovery record, booked without a write."""

    deferred: list[tuple[Target, str]] = field(default_factory=list[tuple[Target, str]])
    """Targets this pass could not reach, and why. Provenance only: the remainder is recomputed next turn."""

    @property
    def done(self) -> list[Target]:
        """Every target the log now carries the ruling for, written now or found written."""
        return [*self.edited, *self.found]


def provenance(
    anomaly: Anomaly, ruling: Ruling, disposition: Disposition
) -> ProvenanceData:
    """Who decided, why, and under which ruling — what every edit carries.

    The ruling's instant is the witness `marked_by` reads back, so it goes into the provenance metadata rather than only the reason text.
    """
    return ProvenanceData(
        author=ruling.by or "steward",
        reason=ruling.reason or None,
        metadata={
            STEWARD: {
                "class": anomaly.class_key,
                "generation": anomaly.generation,
                "ruling": ruling.ts,
                "disposition": disposition.value,
            }
        },
    )


def marked_by(sample: EvalSample, ruling_ts: str) -> bool:
    """Whether any of this sample's scores already carries an edit under this ruling.

    The per-sample crash-recovery witness, one field over from `_landed`'s *found already invalidated*: a runner that wrote the log and died before journaling leaves the effect landed and the record missing, and the next runner must book it rather than edit twice.
    """
    for score in (sample.scores or {}).values():
        for edit in score.history:
            if edit.provenance is None:
                continue
            record = edit.provenance.metadata.get(STEWARD)
            if (
                isinstance(record, dict)
                and cast(dict[str, Any], record).get("ruling") == ruling_ts
            ):
                return True
    return False


def locate(log: EvalLog, target: Target) -> EvalSample | None:
    """The sample this target names, or `None` where the log no longer holds it under that uuid.

    Matched on `(id, epoch)` and then held to the uuid: a sample re-run since the census was taken has a fresh uuid, and editing it under a ruling made about its predecessor would mark data nobody ruled on.
    """
    for sample in log.samples or []:
        if str(sample.id) == target.sample_id and sample.epoch == target.epoch:
            return sample if sample.uuid == target.uuid else None
    return None


def exclude_targets(
    log: EvalLog, targets: Sequence[Target], anomaly: Anomaly, ruling: Ruling
) -> Marked:
    """Write an exclusion with Inspect's `exclude_samples`.

    Every score of each target becomes unscored with the reason `excluded`, and an errored sample that was never scored gets one under the log's first scorer. A target Inspect finds already excluded, by this ruling or another, is booked as found.

    Args:
        log: The task's current log, read whole.
        targets: The ruled samples in it.
        anomaly: The window the ruling closed, for its class and generation.
        ruling: The exclusion.

    Returns:
        What was edited, found already edited, and deferred. Nothing is written.

    Raises:
        ValueError: Inspect refused the exclusion, for example a sample with no score in a log that names no scorer. Nothing is edited.
        LookupError: A metric or epoch reducer of the log is not registered. Nothing is edited.
    """
    marked, pending = _pending(log, targets, ruling)
    if not pending:
        return marked
    result = exclude_samples(
        log, list(pending), provenance(anomaly, ruling, Disposition.EXCLUDE)
    )
    _book(marked, pending, result.applied)
    marked.found.extend(pending[key][0] for key in result.unchanged)
    return marked


def zero_targets(
    log: EvalLog,
    empty: EvalLog,
    targets: Sequence[Target],
    anomaly: Anomaly,
    ruling: Ruling,
) -> Marked:
    """Write a zero with Inspect's `zero_samples`, from the side run's empty-attempt log.

    Each target takes the scores its sample was given in the empty attempt, with the reason `zeroed`; the transcript stays where it is. Inspect zeroes a sample again if asked, so the record that this ruling is already written is Steward's own (`marked_by`).

    Args:
        log: The task's current log, read whole.
        empty: The side run's log of this task, run with `empty_attempt()` and read whole.
        targets: The ruled samples in `log`.
        anomaly: The window the ruling closed.
        ruling: The zero ruling.

    Returns:
        What was edited, found already edited, and deferred. Nothing is written.

    Raises:
        ValueError: Inspect refused the zero: the empty-attempt log does not match, or a target has no counted verdict in it. Nothing is edited.
        LookupError: A metric or epoch reducer of the log is not registered. Nothing is edited.
    """
    marked, pending = _pending(log, targets, ruling)
    if not pending:
        return marked
    result = zero_samples(
        log, empty, list(pending), provenance(anomaly, ruling, Disposition.ZERO)
    )
    _book(marked, pending, result.applied)
    return marked


def _pending(
    log: EvalLog, targets: Sequence[Target], ruling: Ruling
) -> tuple[Marked, dict[tuple[int | str, int], tuple[Target, EvalSample]]]:
    """Sort the targets into those still to edit, keyed as Inspect selects them, and those found or deferred."""
    marked = Marked()
    pending: dict[tuple[int | str, int], tuple[Target, EvalSample]] = {}
    for target in targets:
        sample = locate(log, target)
        if sample is None:
            marked.deferred.append(
                (target, "the log no longer holds it under that uuid")
            )
        elif marked_by(sample, ruling.ts):
            marked.found.append(target)
        else:
            pending[(sample.id, sample.epoch)] = (target, sample)
    return marked, pending


def _book(
    marked: Marked,
    pending: Mapping[tuple[int | str, int], tuple[Target, EvalSample]],
    applied: Sequence[tuple[int | str, int]],
) -> None:
    """Record the targets Inspect edited, and the score names each now holds."""
    for key in applied:
        target, sample = pending[key]
        marked.edited.append(target)
        marked.scores.update(sample.scores or {})


def commit(log: EvalLog, location: str) -> None:
    """Write the edited log back whole.

    Whole rather than header-only, because the edits are in the samples; Inspect has already recomputed the metrics. The etag is populated by a read from S3 and a documented no-op elsewhere, so this is a compare-and-swap remotely and best-effort locally — where the runner's workspace claim is what serializes it against a tend rewriting the same file for a re-run or an acceptance.

    Raises:
        WriteConflictError: The log changed on S3 since it was read. Propagates to the runner, which exits without a record so the tend retries.
    """
    write_eval_log(log, location, if_match_etag=log.etag)


__all__ = [
    "STEWARD",
    "Marked",
    "Target",
    "commit",
    "exclude_targets",
    "locate",
    "marked_by",
    "provenance",
    "zero_targets",
]
