"""The summary rebuild, at the fold itself.

A synthesized compacted parquet is the whole fixture: the rebuild's contract is *derive the summary from the rows*, so the rows are stated and the summary asserted. The end-to-end claim — that `finalize_scan` corrects what two real workers' last-writer-wins accumulation lost — is the live test's (`tests/schedule/test_tend_live.py`), where the undercount actually happens.
"""

import json
from pathlib import Path
from typing import Any

import pyarrow as pa  # pyright: ignore[reportMissingTypeStubs]
import pyarrow.parquet as pq  # pyright: ignore[reportMissingTypeStubs]
from inspect_ai.model import ModelUsage
from inspect_steward._scan import rebuild_summary


def usage(total: int) -> str:
    return json.dumps(
        {"mockllm/model": ModelUsage(total_tokens=total).model_dump(exclude_none=True)}
    )


ROWS: list[dict[str, Any]] = [
    # a truthy boolean verdict, as compaction stringifies it
    {
        "transcript_id": "t1",
        "value": "true",
        "value_type": "boolean",
        "scan_error": None,
        "scan_total_tokens": 10,
        "scan_model_usage": usage(10),
    },
    # a falsy verdict still counts the scan, not the result
    {
        "transcript_id": "t2",
        "value": "false",
        "value_type": "boolean",
        "scan_error": None,
        "scan_total_tokens": 7,
        "scan_model_usage": usage(7),
    },
    # an errored scan: no result, one error
    {
        "transcript_id": "t3",
        "value": None,
        "value_type": "null",
        "scan_error": "boom",
        "scan_total_tokens": 0,
        "scan_model_usage": "{}",
    },
    # a resultset contributes its count of positive items, and a second row
    # for t1 must not count the transcript twice
    {
        "transcript_id": "t1",
        "value": json.dumps([{"value": 1}, {"value": 0}, {"value": True}]),
        "value_type": "resultset",
        "scan_error": None,
        "scan_total_tokens": 3,
        "scan_model_usage": usage(3),
    },
]
"""Four rows over three transcripts: 3 scans, 1 + 2 results, 1 error, 20 tokens."""


SPEC = {
    "scan_id": "run-1",
    "scan_name": "eval_set",
    "scanners": {
        "alpha": {"name": "pkg/alpha"},
        "beta": {"name": "pkg/beta"},
    },
}


def compacted_parquet(rows: list[dict[str, Any]]) -> bytes:
    """A compacted parquet as the finalize leaves it — with an `input` column of transcript-history heft, which the projection must never load."""
    import io

    table = pa.table(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        {
            **{
                column: [row[column] for row in rows]
                for column in (
                    "transcript_id",
                    "value",
                    "value_type",
                    "scan_error",
                    "scan_model_usage",
                )
            },
            "scan_total_tokens": pa.array(  # pyright: ignore[reportUnknownMemberType]
                [row["scan_total_tokens"] for row in rows],
                type=pa.int64(),  # pyright: ignore[reportUnknownMemberType]
            ),
            "input": ["<the whole transcript>" * 1000 for _ in rows],
        }
    )
    buffer = io.BytesIO()
    pq.write_table(  # pyright: ignore[reportUnknownMemberType]
        table,  # pyright: ignore[reportUnknownArgumentType]
        buffer,
    )
    return buffer.getvalue()


def scan_dir_with(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    """A finalized-looking scan directory: spec, compacted parquet, copied summary.

    The prior `_summary.json` carries the counts a lone worker's accumulator would have persisted — wrong — and `complete: false`, which the rebuild must preserve rather than re-decide.
    """
    scan_dir = tmp_path / "scans" / "scan_id=run-1"
    scan_dir.mkdir(parents=True)
    (scan_dir / "_scan.json").write_text(json.dumps(SPEC))
    (scan_dir / "_summary.json").write_text(
        json.dumps(
            {"complete": False, "scanners": {"alpha": {"scans": 1, "results": 1}}}
        )
    )
    (scan_dir / "alpha.parquet").write_bytes(compacted_parquet(rows))
    return scan_dir


def test_the_summary_is_derived_from_the_rows(tmp_path: Path) -> None:
    summary = rebuild_summary(str(scan_dir_with(tmp_path, ROWS)))

    alpha = summary.scanners["alpha"]
    assert alpha.scans == 3
    assert alpha.results == 3
    assert alpha.errors == 1
    assert alpha.tokens == 20
    assert alpha.model_usage["mockllm/model"].total_tokens == 20

    # a scanner that recorded nothing still appears, at zero — it is in the
    # spec, so absence from the report would read as absence from the scan
    assert summary.scanners["beta"] == type(summary.scanners["beta"])()

    # completeness is the finalize's verdict, not the rebuild's
    assert summary.complete is False


def test_the_rebuild_reads_schemes_pyarrow_cannot_infer() -> None:
    """`memory://` stands in for any fsspec-only scheme (`az://`, ...): pyarrow cannot build a filesystem from the URI, so this fold succeeds only through the fsspec branch of the projection."""
    import uuid

    from upath import UPath

    root = UPath(f"memory://{uuid.uuid4().hex}/scans/scan_id=run-1")
    root.mkdir(parents=True)
    (root / "_scan.json").write_text(json.dumps(SPEC))
    (root / "alpha.parquet").write_bytes(compacted_parquet(ROWS))

    summary = rebuild_summary(str(root))
    alpha = summary.scanners["alpha"]
    assert alpha.scans == 3
    assert alpha.results == 3
    assert alpha.errors == 1
    assert alpha.tokens == 20
