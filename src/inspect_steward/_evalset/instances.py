"""Reading anomaly instances out of logs, at a cost that stays flat.

Observation reads headers; classification needs more — an errored sample's traceback lives in the sample, and operator-limit terminations appear only in the sample summaries. This module is the amended read discipline, stated once (it also amends `observe.py`'s "only headers are ever read"):

- **Headers**: every log, every turn, cached by mtime+size (unchanged, `cache.py`).
- **Sample summaries** (`read_eval_log_sample_summaries` — one cheap read per log): once per *settled* log, cached; per turn for a *running* log only when its worker's live read says errors exist.
- **Single samples** (`read_eval_log_sample(..., exclude_fields=...)`): only for errored samples not already classed, once each.
- **Never** transcripts, events, or whole logs — the expensive read the old rule always existed for survives intact.

**Settled logs read summaries unconditionally on first settle**, not only when the header shows errors: an operator-terminated sample is an anomaly instance (workflow.md §14) and the header carries no limit counts at all. The cost story holds anyway — a settled log is immutable, so its summaries are read once per campaign, and steady state (everything settled, nothing new) costs zero reads beyond the listing, exactly like the header cache.

The cache (`.steward/classed.json`) follows `cache.py`'s discipline exactly: version-stamped and discarded on mismatch, never raises, a tend writes it and a `status` only reads, pruned to what the listing still names. Losing it costs one slow turn and never an answer — instance identity is content-derived, so a re-read re-emits what the journal already absorbed and the fold drops it.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from inspect_ai.log import read_eval_log_sample, read_eval_log_sample_summaries
from inspect_ai.scorer import value_to_float

from .classify import (
    MESSAGE_CAP,
    OPERATOR_LIMIT,
    cancelled,
    error_class,
    kind_of,
    substrate,
)
from .classify import (
    parse_error as _parse_error,
)
from .observe import LogAttempt, ObservedLogs, UnreadableLog

CLASSED_VERSION = 1
"""Bumped when `Instance` changes shape. Derived data; rebuilding costs one turn."""

EXCLUDED_FIELDS = frozenset({"messages", "events", "store", "attachments"})
"""What a single-sample read leaves behind. The error and its traceback are what classification needs; the transcript is the read the discipline forbids."""


@dataclass(frozen=True)
class Instance:
    """One anomaly instance, identified so that re-detection is idempotent.

    Carries the sample's `uuid` *and* its `id`/`epoch` *and* the log location per instance, deliberately: invalidation keys on uuids, every `inspect ctl` line keys on id+epoch, and the mismatch between the two is the detail most likely to be missed (step 25's pinned interface).
    """

    class_key: str
    ref: str
    """Content-derived identity — `{eval_id}:{id}:{epoch}:{uuid}` for sample instances, `{identifier}@{marker}` for task attempts — which is what lets the fold dedupe across cache loss."""

    task: str = ""
    """Task identifier."""

    location: str = ""
    """The log, or empty for a worker that left none."""

    message: str = ""
    """Verbatim evidence, truncated. Display only."""

    attempt_created: str = ""
    """When the attempt holding this instance was created — what orders it against a ruling."""

    substrate: bool = False
    eval_id: str = ""
    sample_id: str = ""
    epoch: int = 0
    uuid: str = ""
    limit_reason: str = ""
    retries: int = 0

    @property
    def kind(self) -> str:
        return kind_of(self.class_key)


@dataclass(frozen=True)
class InstanceBatch:
    """Every observed instance of one class, this turn — the seam with the anomaly fold."""

    class_key: str
    kind: str
    substrate: bool
    instances: tuple[Instance, ...]


@dataclass(frozen=True)
class _LogEntry:
    """What one settled log classified to, keyed by what says it changed."""

    mtime: float | None
    total: int
    completed: int
    instances: tuple[Instance, ...]
    zero: bool
    """Whether every score in it converts to zero — the uniform-zero detector's confirming read, taken during the one summaries pass this log ever gets."""


@dataclass
class ClassedCache:
    """What previous turns classified, for the logs that have not changed since.

    Keyed on mtime plus the header's own counts rather than mtime+size — the listing's size is not carried this deep, and the counts are the same mutation signal for the case that matters (an invalidation rewrites the file and moves both).
    """

    logs: dict[str, _LogEntry] = field(default_factory=dict[str, _LogEntry])

    running: dict[str, dict[str, Instance]] = field(
        default_factory=dict[str, dict[str, Instance]]
    )
    """The per-sample memo that survives a running log's constantly moving mtime, so the same errored sample is never single-sample-read twice while its log grows. Keyed by `eval_id`, entries by `id:epoch:uuid` — the uuid so a requeued sample re-classes rather than inheriting its predecessor's key."""

    hits: int = 0
    """Settled reads skipped this run. Never serialized; tests read it."""

    def get(self, attempt: LogAttempt) -> _LogEntry | None:
        entry = self.logs.get(attempt.location)
        if (
            entry is None
            or entry.mtime != attempt.mtime
            or entry.total != attempt.total_samples
            or entry.completed != attempt.completed_samples
        ):
            return None
        self.hits += 1
        return entry

    def keep(self, locations: set[str], running: set[str]) -> "ClassedCache":
        """This cache narrowed to the logs a listing still names and the evals still running."""
        return ClassedCache(
            logs={
                location: entry
                for location, entry in self.logs.items()
                if location in locations
            },
            running={
                eval_id: memo
                for eval_id, memo in self.running.items()
                if eval_id in running
            },
        )


@dataclass(frozen=True)
class ClassedLogs:
    """What classification observed across a directory, this turn."""

    instances: list[Instance] = field(default_factory=list[Instance])
    """Every sample-level instance — `error:` and `limit:` classes."""

    zero: dict[str, bool] = field(default_factory=dict[str, bool])
    """Per settled log location: whether every score in it converts to zero. What the uniform-zero detector reads."""

    unreadable: list[UnreadableLog] = field(default_factory=list[UnreadableLog])
    """Settled logs whose summaries could not be read — real damage, routed to the existing `unreadable` item path. A *running* log that will not read yet is skipped silently instead: it is being written."""


def classed_instances(
    logs: ObservedLogs,
    *,
    errored_running: set[str],
    cache: ClassedCache,
) -> ClassedLogs:
    """Classify every log's errored and operator-limited samples.

    Args:
        logs: The directory, as observation read it.
        errored_running: Locations of running logs whose worker reports errored samples — the gate that keeps a healthy fleet from paying per-turn summaries reads.
        cache: What previous turns classified. Filled in as this turn reads; the caller prunes and writes it back.

    Returns:
        The instances observed, the per-log zero flags, and what could not be read.
    """
    out = ClassedLogs()
    for attempts in logs.attempts.values():
        for attempt in attempts:
            if attempt.status == "started":
                if attempt.location in errored_running:
                    out.instances.extend(_running(attempt, cache))
                continue
            entry = cache.get(attempt)
            if entry is None:
                classified = _settled(attempt)
                if isinstance(classified, UnreadableLog):
                    out.unreadable.append(classified)
                    continue
                entry, complete = classified
                if complete:
                    cache.logs[attempt.location] = entry
                # authoritative now, however it classed: the memo was only ever
                # a stand-in for this read
                cache.running.pop(attempt.eval_id, None)
            out.instances.extend(entry.instances)
            out.zero[attempt.location] = entry.zero
    return out


def _settled(attempt: LogAttempt) -> tuple[_LogEntry, bool] | UnreadableLog:
    """One settled log's classification, and whether it is complete enough to cache.

    A single-sample read that fails degrades that instance to a message-only class rather than losing it — but the log is then not cached, so the next turn retries for the better key. Bounded at one summaries read per turn per such log.
    """
    try:
        summaries = read_eval_log_sample_summaries(attempt.location)
    except Exception as ex:
        return UnreadableLog(
            location=attempt.location,
            reason=f"sample summaries could not be read ({type(ex).__name__}: {ex})",
        )
    instances: list[Instance] = []
    complete = True
    for summary in summaries:
        classed, whole = _classify(attempt, summary)
        complete = complete and whole
        if classed is not None:
            instances.append(classed)
    return (
        _LogEntry(
            mtime=attempt.mtime,
            total=attempt.total_samples,
            completed=attempt.completed_samples,
            instances=tuple(instances),
            zero=_all_zero(summaries),
        ),
        complete,
    )


def _running(attempt: LogAttempt, cache: ClassedCache) -> list[Instance]:
    """A running log's instances: the memo, plus a read for whatever is new.

    Skipped silently on a read failure — a log being written is ordinarily briefly unreadable, and the settled path catches up when it lands.
    """
    try:
        summaries = read_eval_log_sample_summaries(attempt.location)
    except Exception:
        return list(cache.running.get(attempt.eval_id, {}).values())
    memo = cache.running.setdefault(attempt.eval_id, {})
    for summary in summaries:
        key = f"{summary.id}:{summary.epoch}:{summary.uuid or ''}"
        if key in memo:
            continue
        classed, whole = _classify(attempt, summary)
        if classed is None:
            continue
        if whole:
            memo[key] = classed
        else:
            # observed this turn, and re-read next turn for the better key
            return [*memo.values(), classed]
    return list(memo.values())


def _classify(attempt: LogAttempt, summary: Any) -> tuple[Instance | None, bool]:
    """One summary's instance, if it is one, and whether the classification is whole."""
    error = summary.error
    if isinstance(error, str) and error:
        if cancelled(error):
            return None, True
        message, traceback, whole = _sample_error(attempt, summary, error)
        parsed = _parse_error(message, traceback)
        return (
            _instance(
                attempt,
                summary,
                class_key=error_class(message, traceback),
                message=message,
                flagged=substrate(parsed, message),
            ),
            whole,
        )
    if summary.limit == "operator":
        reason = summary.limit_reason if isinstance(summary.limit_reason, str) else ""
        return (
            _instance(
                attempt,
                summary,
                class_key=OPERATOR_LIMIT,
                message=reason[:MESSAGE_CAP],
                flagged=False,
            ),
            True,
        )
    return None, True


def _sample_error(
    attempt: LogAttempt, summary: Any, fallback: str
) -> tuple[str, str | None, bool]:
    """The error's message and traceback, from the sample itself.

    The summary carries the message alone; identity wants the traceback's type and raising frame. A read that fails degrades to the message — usually still a typed key, since upstream records `repr(ex)` — and reports itself as partial so the log is not cached against a retry.
    """
    try:
        sample = read_eval_log_sample(
            attempt.location,
            id=summary.id,
            epoch=summary.epoch,
            exclude_fields=set(EXCLUDED_FIELDS),
        )
    except Exception:
        return fallback, None, False
    error = sample.error
    if error is None:
        return fallback, None, True
    return error.message or fallback, error.traceback, True


def _instance(
    attempt: LogAttempt,
    summary: Any,
    *,
    class_key: str,
    message: str,
    flagged: bool,
) -> Instance:
    uuid = summary.uuid if isinstance(summary.uuid, str) else ""
    retries = summary.retries if isinstance(summary.retries, int) else 0
    reason = summary.limit_reason if isinstance(summary.limit_reason, str) else ""
    return Instance(
        class_key=class_key,
        ref=f"{attempt.eval_id}:{summary.id}:{summary.epoch}:{uuid}",
        task=attempt.identifier,
        location=attempt.location,
        message=message[:MESSAGE_CAP],
        attempt_created=attempt.created,
        substrate=flagged,
        eval_id=attempt.eval_id,
        sample_id=str(summary.id),
        epoch=summary.epoch,
        uuid=uuid,
        limit_reason=reason[:MESSAGE_CAP],
        retries=retries,
    )


_LETTERS = frozenset({"C", "I", "P", "N"})
"""The score letters `value_to_float` maps by default. Any other string means the confirmation abstains rather than guessing — the converter's own fallback would read an unrecognised value as zero, which is precisely the false confirm this read exists to prevent."""


def _all_zero(summaries: list[Any]) -> bool:
    """Whether every score in a log converts to exactly zero.

    The uniform-zero detector's confirming half: a headline of 0.0 produced by cancellation over ±1 scores does not confirm, and a log with no scores at all confirms nothing.
    """
    to_float = value_to_float()
    scored = False
    for summary in summaries:
        scores: dict[str, Any] = summary.scores or {}
        for score in scores.values():
            value = score.value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                converted = float(value)
            elif isinstance(value, str) and value in _LETTERS:
                converted = to_float(value)
            else:
                return False
            scored = True
            if converted != 0.0:
                return False
    return scored


def read_classed_cache(path: Path) -> ClassedCache:
    """Read the cache, treating every failure as an empty one.

    Args:
        path: `.steward/classed.json`.

    Returns:
        What it held, or an empty cache when absent, unreadable, or from another version.
    """
    try:
        loaded: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return ClassedCache()
    if not isinstance(loaded, dict):
        return ClassedCache()
    document = cast(dict[str, object], loaded)
    if document.get("version") != CLASSED_VERSION:
        return ClassedCache()

    cache = ClassedCache()
    logs = document.get("logs")
    if isinstance(logs, dict):
        for location, raw in cast(dict[str, object], logs).items():
            entry = _log_entry(raw)
            if entry is not None:
                cache.logs[location] = entry
    running = document.get("running")
    if isinstance(running, dict):
        for eval_id, raw in cast(dict[str, object], running).items():
            if not isinstance(raw, dict):
                continue
            memo: dict[str, Instance] = {}
            for key, one in cast(dict[str, object], raw).items():
                instance = _read_instance(one)
                if instance is not None:
                    memo[key] = instance
            if memo:
                cache.running[eval_id] = memo
    return cache


def _log_entry(raw: object) -> _LogEntry | None:
    if not isinstance(raw, dict):
        return None
    record = cast(dict[str, object], raw)
    mtime, total, completed = (
        record.get("mtime"),
        record.get("total"),
        record.get("completed"),
    )
    listed = record.get("instances")
    if not isinstance(total, int) or not isinstance(completed, int):
        return None
    instances: list[Instance] = []
    for one in cast(list[object], listed) if isinstance(listed, list) else []:
        instance = _read_instance(one)
        if instance is None:
            # one damaged entry costs one re-read, not the whole cache
            return None
        instances.append(instance)
    return _LogEntry(
        mtime=mtime if isinstance(mtime, (int, float)) else None,
        total=total,
        completed=completed,
        instances=tuple(instances),
        zero=record.get("zero") is True,
    )


def _read_instance(raw: object) -> Instance | None:
    if not isinstance(raw, dict):
        return None
    try:
        return Instance(**cast(dict[str, Any], raw))
    except TypeError:
        return None


def write_classed_cache(path: Path, cache: ClassedCache) -> None:
    """Write the cache, and never at the cost of the turn.

    Args:
        path: `.steward/classed.json`.
        cache: What this turn classified, already pruned by the caller.
    """
    document: dict[str, Any] = {
        "version": CLASSED_VERSION,
        "logs": {
            location: {
                "mtime": entry.mtime,
                "total": entry.total,
                "completed": entry.completed,
                "zero": entry.zero,
                "instances": [asdict(instance) for instance in entry.instances],
            }
            for location, entry in cache.logs.items()
        },
        "running": {
            eval_id: {key: asdict(instance) for key, instance in memo.items()}
            for eval_id, memo in cache.running.items()
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError:
        return


__all__ = [
    "CLASSED_VERSION",
    "EXCLUDED_FIELDS",
    "ClassedCache",
    "ClassedLogs",
    "Instance",
    "InstanceBatch",
    "classed_instances",
    "read_classed_cache",
    "write_classed_cache",
]
