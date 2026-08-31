"""The durable summary, derived from the rows at the terminal fold.

Scout's buffer accumulates `_summary.json` per process, and its persistence is last-writer-wins across the record-only workers that share one buffer — so the file undercounts every worker's share but one, and upstream's finalize *copies* that winning file into the scan directory. The rows themselves are never wrong (one parquet per scanner and transcript, each written by exactly one worker), which means the summary is a materialized view being maintained as an accumulator. Steward is the scan's single writer and, today, the only consumer of these directories — so the fix lives here: after the terminal finalize, the summary is **rebuilt from the compacted rows** and written over the copied one.

Rebuilding is also more truthful than accumulation could ever be. Rows pruned as orphans by the finalize are not counted, where the accumulator counted them when they were recorded; a transcript re-recorded after an error counts once, where the accumulator counted every attempt.

Only the cheap columns are read. The compacted parquet carries the full scanner input beside each verdict — `input` and `input_data` hold entire transcript histories — and parquet's columnar layout means a projection over the six summary columns never touches them, locally or over ranged remote reads.

Not rebuilt: `validation` and `metrics`. The online dispatch records neither (`scan_eval_sample` passes `metrics=None`, and validation rides scout's batch path), so in Steward's path there is nothing to lose — but a summary rebuilt here would drop them if they ever appeared, which is why the omission is stated rather than silent.
"""

import json
from typing import Any, cast

import pyarrow.fs as pafs  # pyright: ignore[reportMissingTypeStubs]
import pyarrow.parquet as pq  # pyright: ignore[reportMissingTypeStubs]
from inspect_ai._eval.task.scan import scan_finalize
from inspect_ai._util._async import run_coroutine
from inspect_ai.model import ModelUsage

# scout's summary internals are a wire format Steward rebuilds, deliberately
# not public API (the same posture as the capture and overrides models); the
# constants and the truthiness/usage helpers are imported rather than copied
# so the two ends cannot drift
from inspect_scout import Summary
from inspect_scout._recorder.buffer import SCAN_SUMMARY
from inspect_scout._recorder.file import SCAN_JSON
from inspect_scout._recorder.summary import ScannerSummary, add_model_usage
from inspect_scout._validation.validate import is_positive_value
from upath import UPath

from .bracket import scan_dir_location

SUMMARY_COLUMNS = (
    "transcript_id",
    "value",
    "value_type",
    "scan_error",
    "scan_total_tokens",
    "scan_model_usage",
)
"""The projection the rebuild reads — everything a `ScannerSummary` derives from, and none of the columns that carry transcript histories."""


def finalize_scan(*, log_dir: str, scan_id: str, scans: str | None = None) -> Summary:
    """The terminal act of the bracket: upstream's finalize, then the summary derived from what survived.

    Upstream's `scan_finalize` folds the buffer into the compacted parquets, prunes orphan rows, and snapshots the transcripts — but the summary it leaves is a copy of the buffer's last-writer-wins file (see the module docstring). This rewrites it from the rows, so what a reader finds beside the compacted results is exact.

    Idempotent, like the finalize it wraps: a re-run folds nothing new and derives the same summary from the same rows.

    Args:
        log_dir: The run's log directory.
        scan_id: The scan id (the run's eval set id).
        scans: The definition's scans redirect (`Manifest.scan.scans`), or `None` for the default under `log_dir`.

    Returns:
        The rebuilt summary, as written to the scan directory.
    """
    run_coroutine(scan_finalize(scan_id=scan_id, log_dir=log_dir, scans=scans))
    scan_dir = scan_dir_location(log_dir=log_dir, scan_id=scan_id, scans=scans)
    summary = rebuild_summary(scan_dir)
    _write_atomic(
        UPath(scan_dir) / SCAN_SUMMARY,
        summary.model_dump_json(indent=2).encode("utf-8"),
    )
    return summary


def rebuild_summary(scan_dir: str) -> Summary:
    """A scan directory's summary, derived from its compacted rows.

    Scanner names come from `_scan.json` — the file the bracket owns — so a scanner that recorded nothing yet still appears, with zero counts, rather than vanishing from the report. The `complete` flag is preserved from the summary the finalize just wrote: completeness is the finalize's verdict (errors leave a scan resumable), and this rebuild replaces the counts, not the verdict.
    """
    root = UPath(scan_dir)
    with (root / SCAN_JSON).open("r") as f:
        names = sorted(json.load(f).get("scanners", {}))

    complete = False
    summary_file = root / SCAN_SUMMARY
    if summary_file.exists():
        with summary_file.open("r") as f:
            complete = bool(json.load(f).get("complete", False))

    return Summary(
        complete=complete,
        scanners={name: _folded(root / f"{name}.parquet") for name in names},
    )


def _folded(parquet: UPath) -> ScannerSummary:
    """One scanner's summary from its compacted parquet, absent meaning nothing recorded."""
    if not parquet.exists():
        return ScannerSummary()

    transcripts: set[str] = set()
    results = 0
    errors = 0
    tokens = 0
    usage: dict[str, ModelUsage] = {}
    for row in _rows(parquet):
        if row["transcript_id"]:
            transcripts.add(row["transcript_id"])
        if row["scan_error"] is not None:
            errors += 1
        results += _truthy(row["value"], row["value_type"])
        tokens += int(row["scan_total_tokens"] or 0)
        used = cast(dict[str, Any], json.loads(row["scan_model_usage"] or "{}"))
        for model, spent in used.items():
            usage[model] = add_model_usage(
                usage.get(model, ModelUsage()), ModelUsage.model_validate(spent)
            )
    return ScannerSummary(
        scans=len(transcripts),
        results=results,
        errors=errors,
        tokens=tokens,
        model_usage=usage,
    )


def _rows(parquet: UPath) -> list[dict[str, Any]]:
    """The projected rows — and only them: the read never touches the history columns.

    The filesystem dispatch mirrors scout's own (`_parquet_source`): the schemes pyarrow supports natively read by HTTP range, so the projection fetches only its columns' byte ranges; any other remote scheme reads through fsspec's file object, whose seeks become ranged reads where the store supports them.
    """
    columns = list(SUMMARY_COLUMNS)
    path = parquet.as_posix()
    if path.startswith(("s3://", "gs://", "gcs://", "abfs://", "abfss://")):
        fs, fs_path = pafs.FileSystem.from_uri(path)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        table = pq.read_table(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            fs_path,  # pyright: ignore[reportUnknownArgumentType]
            columns=columns,
            filesystem=fs,  # pyright: ignore[reportUnknownArgumentType]
        )
    elif parquet.protocol not in ("", "file"):
        with parquet.open("rb") as f:
            table = pq.read_table(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                f, columns=columns
            )
    else:
        table = pq.read_table(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            path, columns=columns
        )
    return cast(
        "list[dict[str, Any]]",
        table.to_pylist(),  # pyright: ignore[reportUnknownMemberType]
    )


def _truthy(value: Any, value_type: Any) -> int:
    """What one row contributes to `results` — the read-side twin of `Summary._report`.

    The compaction forces the `value` column to string (mixed types across files), so a typed value arrives here as its string form and `value_type` says how to read it. A resultset contributes its count of positive items, exactly as the accumulator counted it; everything else contributes one when truthy.
    """
    if value is None:
        return 0
    if isinstance(value, bool | int | float):
        # buffer files hold native types; tolerated in case a caller ever
        # folds one directly
        return 1 if value else 0
    match value_type:
        case "boolean":
            return 1 if value.lower() == "true" else 0
        case "number":
            try:
                return 1 if float(value) else 0
            except ValueError:
                return 0
        case "string":
            return 1 if value else 0
        case "resultset":
            items = cast("list[Any]", json.loads(value))
            return sum(
                1
                for item in items
                if isinstance(item, dict)
                and is_positive_value(cast("dict[str, Any]", item).get("value"))
            )
        case "array" | "object":
            return 1 if json.loads(value) else 0
        case _:
            return 0


def _write_atomic(target: UPath, data: bytes) -> None:
    """Write so a concurrent reader sees the previous summary or this one.

    The same discipline as scout's own sync: a remote object store exposes a PUT atomically, while a local filesystem exposes partial writes — so locally the write goes to a uniquely-named sibling and renames over the target.
    """
    import os
    import uuid

    if target.protocol not in ("", "file"):
        target.write_bytes(data)
        return
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp.as_posix(), target.as_posix())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
