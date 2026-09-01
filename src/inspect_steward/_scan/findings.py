"""Scan rows read as anomaly instances — the sixth detection signature.

A scanner result is a *reading*, and workflow.md §12.6 is right that no threshold Steward could apply to an arbitrary one would mean anything. But a **boolean** scanner has already done the judging: its author wrote a question with a yes and a no, and `true` is the yes. So this module reads exactly that shape and nothing else — a row whose `value_type` is not `boolean` is passed over, visibly, rather than thresholded into a verdict nobody asked for. Steward's built-in `scoring_integrity` is one of these, and its `label` is what makes the class say which kind of concern it is.

**The row already carries Steward's whole instance identity, which is the reason this is cheap.** Upstream composes a transcript's info from the sample itself (`transcript_info_from_eval_sample`), so `transcript_source_id` is the eval id, `transcript_task_id` the sample id, `transcript_task_repeat` the epoch and `transcript_id` the sample uuid — the four parts of `Instance.ref`, in that order. Nothing has to be read back out of a log to compose one, and a finding therefore costs a narrow columnar projection and no eval-log reads at all.

**One parquet per scanner, and one failing file costs its scanner — visibly.** A directory of them is read scanner by scanner, so a file written by a version whose schema this one does not know takes its own scanner's findings down and leaves the rest of the census intact. But it is *reported*, on `_evalset.instances`' reasoning about a log that will not read: a parquet nobody could open is indistinguishable from a scanner that flagged nothing, and swallowing it would let a run be signed on the assumption that nothing was found. So a read failure becomes an `UnreadableLog` and travels the path that already exists for one — the agent's item, the signoff blocker, and the acknowledgment that turns it into a named caveat.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from upath import UPath

from .._evalset.classify import MESSAGE_CAP, scan_class
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
)
"""The projection a finding needs: the four identity columns, the verdict, and the explanation that travels as evidence.

Deliberately not `input` / `input_data` / `scan_events` — scout calls those the heavy columns because each can hold an entire transcript history, and the whole affordability of re-reading this every turn is that a columnar projection never touches them.
"""


@dataclass(frozen=True)
class ScanFindings:
    """What one read of a scan directory produced, and what it could not read."""

    instances: list[Instance] = field(default_factory=list[Instance])
    """One instance per flagged row, in no particular order — `detect` batches and sorts."""

    unreadable: list[UnreadableLog] = field(default_factory=list[UnreadableLog])
    """Scanners whose rows would not read. Merged into the turn's unreadable set, which is what makes them a blocker rather than a silence."""

    incomplete: dict[str, int] = field(default_factory=dict[str, int])
    """Per scanner, transcripts it recorded an error against rather than a verdict.

    **The rows this module reads past, counted rather than dropped.** A scanner that threw is a signature of its own — keyed on scanner plus exception type — and building it is still step 29's; what cannot wait is that an errored transcript is a transcript *nobody scanned*, and its row is indistinguishable here from a scanner that looked and found nothing. Left uncounted, a run whose every scan errored reads exactly like a run that came back clean, and the signature says so.
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
        The findings, and any scanner whose parquet would not read.
    """
    root = UPath(scan_dir)
    found = ScanFindings()
    for scanner in scanners:
        parquet = root / f"{scanner}.parquet"
        if not parquet.exists():
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
            continue
        errored = sum(1 for row in rows if row["scan_error"] is not None)
        if errored:
            found.incomplete[scanner] = errored
        found.instances.extend(_instances(rows, scanner=scanner, attempts=attempts))
    return found


def _instances(
    rows: list[dict[str, Any]],
    *,
    scanner: str,
    attempts: Mapping[str, LogAttempt],
) -> list[Instance]:
    """The flagged rows of one scanner's parquet, as instances."""
    instances: list[Instance] = []
    for row in rows:
        if row["scan_error"] is not None:
            # a scanner that threw is a different signature — keyed on scanner
            # plus exception type — and is not this module's business
            continue
        if not flagged(row["value"], row["value_type"]):
            continue
        attempt = attempts.get(log_key(row["transcript_source_uri"]))
        if attempt is None:
            continue
        eval_id = _text(row["transcript_source_id"])
        sample_id = _text(row["transcript_task_id"])
        epoch = row["transcript_task_repeat"]
        epoch = epoch if isinstance(epoch, int) else 0
        uuid = _text(row["transcript_id"])
        label = _text(row["label"])
        instances.append(
            Instance(
                class_key=scan_class(scanner, label or None),
                # byte-identical to what `_evalset.instances._instance` composes
                # for the same sample: the two are joined by nothing but this
                # format, so they are pinned against each other by a test
                ref=f"{eval_id}:{sample_id}:{epoch}:{uuid}",
                task=attempt.identifier,
                location=attempt.location,
                message=_text(row["explanation"])[:MESSAGE_CAP],
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
