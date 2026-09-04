"""Scan rows read as anomaly instances — the sixth detection signature.

A scanner result is a *reading*, and workflow.md §12.6 is right that no threshold Steward could apply to an arbitrary one would mean anything. But a **boolean** scanner has already done the judging: its author wrote a question with a yes and a no, and `true` is the yes. So this module reads exactly that shape and nothing else — a row whose `value_type` is not `boolean` is passed over, visibly, rather than thresholded into a verdict nobody asked for. Steward's built-in `scoring_integrity` is one of these, and its `label` is what makes the class say which kind of concern it is.

**The row already carries Steward's whole instance identity, which is the reason this is cheap.** Upstream composes a transcript's info from the sample itself (`transcript_info_from_eval_sample`), so `transcript_source_id` is the eval id, `transcript_task_id` the sample id, `transcript_task_repeat` the epoch and `transcript_id` the sample uuid — the four parts of `Instance.ref`, in that order. Nothing has to be read back out of a log to compose one, and a finding therefore costs a narrow columnar projection and no eval-log reads at all.

**A scanner that threw is the seventh signature, off the same projection.** The parquet records a row for a transcript the scanner could not read, carrying the exception rather than a verdict, and that row is composed into an instance of its own kind (`scanerror:`) rather than dropped. Same window, same rulings, same census: *scanning is broken here* is one class spanning five hundred transcripts, and *this transcript breaks the scanner* is a class of one, which is the whole distinction anybody needs and nothing has to compute it.

**One parquet per scanner, and one failing file costs its scanner — visibly.** A directory of them is read scanner by scanner, so a file written by a version whose schema this one does not know takes its own scanner's findings down and leaves the rest of the census intact. But it is *reported*, on `_evalset.instances`' reasoning about a log that will not read: a parquet nobody could open is indistinguishable from a scanner that flagged nothing, and swallowing it would let a run be signed on the assumption that nothing was found. So a read failure becomes an `UnreadableLog` and travels the path that already exists for one — the agent's item, the signoff blocker, and the acknowledgment that turns it into a named caveat.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from upath import UPath

from .._evalset.classify import MESSAGE_CAP, scan_class, scan_error_class
from .._evalset.instances import Instance
from .._evalset.observe import LogAttempt, UnreadableLog
from .summary import read_columns

FINDING_COLUMNS = (
    "transcript_id",
    "transcript_source_id",
    "transcript_source_uri",
    "transcript_task_id",
    "transcript_task_repeat",
    "value",
    "value_type",
    "label",
    "explanation",
    "scan_error",
    "scan_error_traceback",
)
"""The projection a finding needs: the four identity columns, the verdict, the explanation that travels as evidence, and the two error columns that carry a scanner's own failure.

**Of scout's three error columns only two are worth reading.** `scan_error` is `str(ex)` rather than `repr(ex)`, so the message alone will not class; `scan_error_type` is the literal `"refusal"` on every error row regardless of what threw. `scan_error_traceback` is a real `traceback.format_exc()`, which is what `classify.scan_error_class` parses.

Deliberately not `input` / `input_data` / `scan_events` — scout calls those the heavy columns because each can hold an entire transcript history, and the whole affordability of re-reading this every turn is that a columnar projection never touches them.
"""


@dataclass(frozen=True)
class ScanFindings:
    """What one read of a scan directory produced, and what it could not read."""

    instances: list[Instance] = field(default_factory=list[Instance])
    """One instance per flagged row and per errored row, in no particular order — `detect` batches and sorts."""

    unreadable: list[UnreadableLog] = field(default_factory=list[UnreadableLog])
    """Scanners whose rows would not read. Merged into the turn's unreadable set, which is what makes them a blocker rather than a silence."""

    recorded: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    """Per task identifier, the transcripts every scanner has recorded — coverage's numerator.

    **A transcript counts once each scanner has answered for it**, which is upstream's own resume predicate (`all(tid in s for s in scanned_per_scanner.values())`) rather than a rule invented here: a sample two of three scanners reached is a sample the third will be sent back for. An **errored** row still counts as recorded, because coverage is *recorded rows against landed samples* and a scanner that threw is its own class — counting it here as well would report one failure twice.
    """


def scan_findings(
    scan_dir: str,
    *,
    scanners: tuple[str, ...],
    attempts: Mapping[str, LogAttempt],
) -> ScanFindings:
    """Every flagged sample the scan directory holds, as anomaly instances.

    Args:
        scan_dir: The run's scan directory.
        scanners: The scanner names this run records under, from the committed merge — so a scanner that has recorded nothing yet is absent rather than an error.
        attempts: The run's own log attempts, keyed by log **basename**. A row naming a log this mapping does not hold is dropped: it belongs to an orphan on its way to the archive, and an orphan's findings are not this run's. A *superseded* attempt's rows are kept, exactly as `classed_instances` keeps a superseded log's errored samples — the census carries the run's whole history and the narrowing to what is in the results happens once, at the reporting layer (`rulings.affected_refs`).

    Returns:
        The findings, the transcripts each task has recorded, and any scanner whose parquet would not read.
    """
    root = UPath(scan_dir)
    found = ScanFindings()
    answered: list[dict[str, set[str]]] = []
    for scanner in scanners:
        parquet = root / f"{scanner}.parquet"
        if not parquet.exists():
            # **an empty answer, not an absent one**, and the difference is the
            # whole of what coverage is for. A scanner in the committed merge
            # with no file at all has answered for nothing, so it covers
            # nothing -- and dropping it from the intersection would let one
            # scanner that ran report the run as fully scanned while another
            # never started. That is the added-scanner gap (exec §13 item 9)
            # and it is exactly what must stay visible
            answered.append({})
            continue
        try:
            rows = read_columns(parquet, FINDING_COLUMNS)
        except Exception as ex:
            # a file this version cannot project costs its own scanner and
            # never the census -- but it is reported rather than swallowed,
            # because unread and unflagged are not the same answer
            found.unreadable.append(
                UnreadableLog(
                    location=parquet.as_posix(),
                    reason=f"{type(ex).__name__}: {ex}",
                    what=f"{scanner}'s scan results",
                )
            )
            # and it covers nothing either, for the stronger version of the
            # same reason: what this file said is unknown, so nothing in it can
            # be counted as answered. The run is already refused over the
            # unreadable file; the coverage figure simply does not claim
            # otherwise while it stands
            answered.append({})
            continue
        answered.append(_answered(rows, attempts=attempts))
        found.instances.extend(_instances(rows, scanner=scanner, attempts=attempts))
    found.recorded.update(_covered(answered))
    return found


def _answered(
    rows: list[dict[str, Any]], *, attempts: Mapping[str, LogAttempt]
) -> dict[str, set[str]]:
    """One scanner's transcripts, per task — read off the rows already in hand.

    Free: every row of the projection carries the log it names and the transcript it is about, and the caller's `attempts` map already turns the first into a task. A row naming a log the run does not own is dropped exactly as a finding is.
    """
    answered: dict[str, set[str]] = {}
    for row in rows:
        attempt = attempts.get(log_key(row["transcript_source_uri"]))
        if attempt is None:
            continue
        answered.setdefault(attempt.identifier, set()).add(_text(row["transcript_id"]))
    return answered


def _covered(answered: list[dict[str, set[str]]]) -> dict[str, set[str]]:
    """The transcripts *every* scanner answered for, per task — the intersection.

    **One entry per configured scanner, empty ones included**, which is the whole reason this is an intersection rather than a union. A scanner with no parquet has answered for nothing; contributing nothing instead of an empty set would take it out of the intersection entirely and let the scanners that *did* run report the run as fully covered on its behalf. That is precisely the shape of the two failures coverage exists to show — a scanner added at a re-launch that will never revisit the transcripts already landed, and a scanner that never started at all.

    The caller is what guarantees the entries are one per scanner; with no scanners at all there is nothing to intersect and nothing is covered.
    """
    if not answered:
        return {}
    tasks: set[str] = set()
    for one in answered:
        tasks.update(one)
    covered: dict[str, set[str]] = {}
    for task in tasks:
        sets: list[set[str]] = [one.get(task, set()) for one in answered]
        covered[task] = set[str].intersection(*sets)
    return covered


def _instances(
    rows: list[dict[str, Any]],
    *,
    scanner: str,
    attempts: Mapping[str, LogAttempt],
) -> list[Instance]:
    """One scanner's parquet as instances — what it flagged, and what it could not read.

    **Two signatures off one projection**, because the rows are the same rows and the difference is one column. A row with `scan_error` set is a transcript the scanner never reached a verdict on, which is a failure of the scanning rather than a finding about the sample; a row without one is read for its verdict.
    """
    instances: list[Instance] = []
    for row in rows:
        errored = row["scan_error"] is not None
        if not errored and not flagged(row["value"], row["value_type"]):
            continue
        attempt = attempts.get(log_key(row["transcript_source_uri"]))
        if attempt is None:
            continue
        eval_id = _text(row["transcript_source_id"])
        sample_id = _text(row["transcript_task_id"])
        epoch = row["transcript_task_repeat"]
        epoch = epoch if isinstance(epoch, int) else 0
        uuid = _text(row["transcript_id"])
        if errored:
            class_key = scan_error_class(scanner, row["scan_error_traceback"])
            message = _text(row["scan_error"])
        else:
            class_key = scan_class(
                scanner,
                _text(row["label"]) or None,
                task=attempt.task,
                identifier=attempt.identifier,
            )
            message = _text(row["explanation"])
        instances.append(
            Instance(
                class_key=class_key,
                # byte-identical to what `_evalset.instances._instance` composes
                # for the same sample: the two are joined by nothing but this
                # format, so they are pinned against each other by a test
                ref=f"{eval_id}:{sample_id}:{epoch}:{uuid}",
                task=attempt.identifier,
                location=attempt.location,
                message=message[:MESSAGE_CAP],
                attempt_created=attempt.created,
                eval_id=eval_id,
                sample_id=sample_id,
                epoch=epoch,
                uuid=uuid,
            )
        )
    return instances


def flagged(value: Any, value_type: Any) -> bool:
    """Whether one row is a scanner saying yes.

    **Boolean only**, and the narrowness is the design: a number, a string or a resultset is a measurement whose meaning lives with whoever wrote the scanner, and reading one here would be Steward inventing a threshold. Those rows are recorded, readable, and simply not escalated by this path.

    The compaction forces the `value` column to string (the column is mixed-type across files), so a boolean arrives as `"true"` or `"false"`; a native bool is tolerated in case a caller ever folds a buffer file directly, which is `summary._truthy`'s allowance for the same reason.
    """
    if value_type != "boolean":
        return False
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.lower() == "true"


def log_key(location: Any) -> str:
    """A log location reduced to what both sides of the join can agree on.

    The two spellings are composed independently — a worker records `absolute_file_path(log_location)` from inside the eval, while observation gets its location from `list_eval_logs` over the resolved log directory — and they disagree in ways that are invisible until every finding silently vanishes: a symlinked temporary directory resolved on one side only, a trailing separator, a URI against a path.

    The basename is what survives all of it and is unique by construction, since a log's filename carries its timestamp, task and eval id. Scoped to one run's own log directory, which is the only place either side is looking.
    """
    return str(location).replace("\\", "/").rsplit("/", 1)[-1]


def _text(value: Any) -> str:
    """A column read as text, with parquet's nulls flattened to empty."""
    return value if isinstance(value, str) else ""


__all__ = [
    "FINDING_COLUMNS",
    "ScanFindings",
    "flagged",
    "log_key",
    "scan_findings",
]
