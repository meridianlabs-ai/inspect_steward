"""Writing a marking ruling into a landed log.

`exclude` and `zero` used to be journal marks: the tend folded them into the by-task table and the *Scores are over N of M* note, and the log kept whatever score the sample had. Upstream's landed-log edit — `edit_score` applying a `ScoreEdit` with history and provenance, `Score.unscored` as the NaN every metric and reducer skips, `recompute_metrics` rebuilding `results` — is what lets the decision travel inside the file, so every reader of the directory sees the result the operator signed rather than the wreckage under it.

**Exclusion is a value Steward can write; zero is not.** An excluded sample becomes unscored on each of its scores, with the ruling's reason. What a *zero* is depends on the task's scorer — `I` for `exact`, `0` for `match`, a grader's own word for a rubric — so the runner obtains it by scoring an empty attempt in a scratch side run (`run.py`), and this module copies that verdict in.

**Every function here is pure over an `EvalLog` read whole.** `edit_score` needs the samples and inserts a `ScoreEditEvent` into the sample's events, so a header-only read has nothing to edit and an `exclude_fields` read would write back a truncated transcript. `commit` is the one write, and it is the runner's to call under the workspace claim.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from inspect_ai.log import (
    EvalLog,
    EvalSample,
    ProvenanceData,
    edit_score,
    recompute_metrics,
    write_eval_log,
)
from inspect_ai.scorer import ScoreEdit

from .._anomaly.model import Anomaly, Disposition, Ruling
from .._evalset.instances import Instance

EXCLUDED = "excluded"
"""The `Score.reason` an exclusion writes. A custom word beside inspect's own `ScoreReason` vocabulary, which the field admits."""

ZEROED = "zeroed"
"""The `Score.reason` a harvested zero writes."""

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


def mark_unscored(
    log: EvalLog, targets: Sequence[Target], anomaly: Anomaly, ruling: Ruling
) -> Marked:
    """Write an exclusion: every score of every target becomes unscored, with the reason.

    Each of a sample's scores is edited rather than one of them, because a multi-scorer task's metrics are per scorer and an exclusion is of the sample. An errored sample that was never scored gets an unscored score named after the task's first scorer, so the exclusion is visible where a reader looks for a score; a log that names no scorer has nowhere to record it, and the target is deferred with that said.

    The scorer's own metadata is left as it stands rather than replaced: a grader's transcript is evidence about the sample, and an exclusion is a decision about it, not a correction of it. The pre-edit score is `history[0]`.

    Args:
        log: The task's current log, read whole.
        targets: The ruled samples in it.
        anomaly: The window the ruling closed, for its class and generation.
        ruling: The exclusion.

    Returns:
        What was edited, found already edited, and deferred. Nothing is written.
    """
    marked = Marked()
    stamp = provenance(anomaly, ruling, Disposition.EXCLUDE)
    for target in targets:
        sample = locate(log, target)
        if sample is None:
            marked.deferred.append(
                (target, "the log no longer holds it under that uuid")
            )
            continue
        if marked_by(sample, ruling.ts):
            marked.found.append(target)
            continue
        names = list(sample.scores or {}) or _first_scorer(log)
        if not names:
            marked.deferred.append((target, "the log names no scorer to record it on"))
            continue
        for name in names:
            edit_score(
                log,
                sample.id,
                name,
                ScoreEdit(
                    value=float("nan"),
                    explanation=f"{ruling.reason} — excluded by {ruling.by or 'steward'}",
                    reason=EXCLUDED,
                    provenance=stamp,
                ),
                recompute_metrics=False,
                epoch=sample.epoch,
            )
            marked.scores.add(name)
        marked.edited.append(target)
    return marked


def harvest_scores(
    log: EvalLog,
    scored: Mapping[Target, EvalSample],
    anomaly: Anomaly,
    ruling: Ruling,
) -> Marked:
    """Write a zero: each target takes the scores its side-run sample was given.

    Value, answer, explanation and metadata are copied as the scorer produced them over the empty attempt, so the log reads exactly as it would had the sample done nothing — which is what the ruling decided it should read as. The transcript stays where it is; only the scores move, with their history.

    Args:
        log: The task's current log, read whole.
        scored: Each ruled sample, paired with the side run's record of it.
        anomaly: The window the ruling closed.
        ruling: The zero ruling.

    Returns:
        What was edited, found already edited, and deferred. Nothing is written.
    """
    marked = Marked()
    stamp = provenance(anomaly, ruling, Disposition.ZERO)
    for target, side in scored.items():
        sample = locate(log, target)
        if sample is None:
            marked.deferred.append(
                (target, "the log no longer holds it under that uuid")
            )
            continue
        if marked_by(sample, ruling.ts):
            marked.found.append(target)
            continue
        if not side.scores:
            marked.deferred.append((target, "the side run recorded no score for it"))
            continue
        for name, score in side.scores.items():
            edit_score(
                log,
                sample.id,
                name,
                ScoreEdit(
                    value=score.value,
                    answer=score.answer,
                    explanation=score.explanation,
                    reason=ZEROED,
                    metadata=score.metadata or {},
                    provenance=stamp,
                ),
                recompute_metrics=False,
                epoch=sample.epoch,
            )
            marked.scores.add(name)
        marked.edited.append(target)
    return marked


def commit(log: EvalLog, location: str) -> None:
    """Recompute the metrics over what the log now holds, and write it back whole.

    The one write. Whole rather than header-only, because the edits are in the samples. The etag is populated by a read from S3 and a documented no-op elsewhere, so this is a compare-and-swap remotely and best-effort locally — where the runner's workspace claim is what serializes it against a tend rewriting the same file for a re-run or an acceptance.

    Raises:
        ValueError: The log names no scorer, so there is nothing to recompute the metrics from — refused rather than written with empty results.
        WriteConflictError: The log changed on S3 since it was read. Propagates to the runner, which exits without a record so the tend retries.
    """
    if not log.eval.scorers:
        raise ValueError(
            "the log names no scorer, so its metrics cannot be recomputed after "
            "the edit"
        )
    recompute_metrics(log)
    write_eval_log(log, location, if_match_etag=log.etag)


def _first_scorer(log: EvalLog) -> list[str]:
    """The name to record an unscored score under, for a sample that was never scored."""
    for scorer in log.eval.scorers or []:
        if scorer.name:
            return [scorer.name]
    return []


__all__ = [
    "EXCLUDED",
    "STEWARD",
    "ZEROED",
    "Marked",
    "Target",
    "commit",
    "harvest_scores",
    "locate",
    "mark_unscored",
    "marked_by",
    "provenance",
]
