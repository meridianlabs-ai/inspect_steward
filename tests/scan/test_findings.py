"""Reading a scan row as an anomaly instance.

A synthesized compacted parquet is the whole fixture, on `test_summary.py`'s grounds exactly: the contract is *this shape of row becomes this instance*, so the rows are stated and the instances asserted. What a real scanner puts in them is `test_bracket.py`'s and the live test's business.

The one thing here that is not about this module is `test_a_findings_ref_is_the_same_string_the_log_read_composes`: two independent composers agree on `Instance.ref` by convention and by nothing else, and a drift between them would silently give one sample two identities — a window that never dedupes and a ruling that covers nothing.
"""

import io
import json
from pathlib import Path
from typing import Any

import pyarrow as pa  # pyright: ignore[reportMissingTypeStubs]
import pyarrow.parquet as pq  # pyright: ignore[reportMissingTypeStubs]
import pytest
from inspect_steward._evalset.observe import LogAttempt
from inspect_steward._scan import scan_findings
from inspect_steward._scan.findings import log_key

SCANNERS = ("scoring_integrity", "quiet")

COLUMNS = (
    "transcript_id",
    "transcript_source_id",
    "transcript_source_uri",
    "transcript_task_id",
    "value",
    "value_type",
    "label",
    "explanation",
    "scan_error",
)
"""Every finding column but the epoch, which is typed and so built separately."""

LOG = "/logs/2026-08-31T10-00-00_cybench_aaa.eval"


def row(
    *,
    value: str | None = "true",
    value_type: str = "boolean",
    label: str | None = "reward_hacking",
    uri: str = LOG,
    uuid: str = "u1",
    sample: str = "s1",
    epoch: int = 1,
    explanation: str = "read the grader at [M12]",
    scan_error: str | None = None,
) -> dict[str, Any]:
    return {
        "transcript_id": uuid,
        "transcript_source_id": "eval-1",
        "transcript_source_uri": uri,
        "transcript_task_id": sample,
        "transcript_task_repeat": epoch,
        "value": value,
        "value_type": value_type,
        "label": label,
        "explanation": explanation,
        "scan_error": scan_error,
    }


def scan_dir_with(tmp_path: Path, rows: list[dict[str, Any]]) -> str:
    """A scan directory holding one scanner's compacted parquet.

    The `input` column carries transcript-history heft, exactly as a real one does, so a projection that stopped being a projection would show up as a test that got slow rather than as nothing at all.
    """
    scan_dir = tmp_path / "scans" / "scan_id=run-1"
    scan_dir.mkdir(parents=True)
    table = pa.table(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        {
            **{column: [entry[column] for entry in rows] for column in COLUMNS},
            "transcript_task_repeat": pa.array(  # pyright: ignore[reportUnknownMemberType]
                [entry["transcript_task_repeat"] for entry in rows],
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
    (scan_dir / "scoring_integrity.parquet").write_bytes(buffer.getvalue())
    return str(scan_dir)


def attempt(location: str = LOG) -> LogAttempt:
    return LogAttempt(
        location=location,
        identifier="cybench@openai/gpt-5",
        created="2026-08-31T10:00:00+00:00",
        status="success",
        invalidated=False,
        error=None,
        total_samples=10,
        completed_samples=10,
        epochs=1,
        task="cybench",
        task_id="task-1",
        eval_id="eval-1",
        mtime=None,
    )


def attempts(location: str = LOG) -> dict[str, LogAttempt]:
    return {log_key(location): attempt(location)}


def found(tmp_path: Path, rows: list[dict[str, Any]]) -> list[Any]:
    return scan_findings(
        scan_dir_with(tmp_path, rows), scanners=SCANNERS, attempts=attempts()
    ).instances


VALUES = [
    # the one shape this path reads, and every shape it deliberately does not
    ("a true boolean", "true", "boolean", True),
    ("a false boolean", "false", "boolean", False),
    ("a boolean spelled in caps", "True", "boolean", True),
    ("a number, however large", "0.97", "number", False),
    ("a non-empty string", "reward hacking", "string", False),
    ("a resultset with positive items", json.dumps([{"value": 1}]), "resultset", False),
    ("a null", None, "null", False),
]


@pytest.mark.parametrize(
    ("value", "value_type", "flags"),
    [(value, value_type, flags) for _, value, value_type, flags in VALUES],
    ids=[case for case, _, _, _ in VALUES],
)
def test_only_a_boolean_yes_is_a_finding(
    value: str | None, value_type: str, flags: bool, tmp_path: Path
) -> None:
    # a number is a measurement whose meaning lives with whoever wrote the
    # scanner; reading one here would be Steward inventing a threshold
    findings = found(tmp_path, [row(value=value, value_type=value_type)])

    assert len(findings) == (1 if flags else 0)


def test_a_finding_carries_the_class_the_label_names(tmp_path: Path) -> None:
    findings = found(tmp_path, [row(label="internet_egress")])

    assert findings[0].class_key == "scan:scoring_integrity:internet_egress"
    assert findings[0].kind == "scan"


def test_a_scanner_that_sets_no_label_classes_on_its_own_name(tmp_path: Path) -> None:
    findings = found(tmp_path, [row(label=None)])

    assert findings[0].class_key == "scan:scoring_integrity"


def test_a_finding_carries_the_explanation_as_evidence(tmp_path: Path) -> None:
    findings = found(tmp_path, [row(explanation="it read the grader at [M12]")])

    assert findings[0].message == "it read the grader at [M12]"


def test_a_finding_is_attributed_to_the_task_whose_log_it_names(
    tmp_path: Path,
) -> None:
    # the one thing the row cannot say: a scan row names a log, and only the
    # observation knows which task that log belongs to
    findings = found(tmp_path, [row()])

    assert findings[0].task == "cybench@openai/gpt-5"
    assert findings[0].location == LOG
    assert findings[0].attempt_created == "2026-08-31T10:00:00+00:00"


def test_a_scanner_that_threw_is_not_a_finding(tmp_path: Path) -> None:
    # a scanner erroring is a different signature entirely — keyed on scanner
    # plus exception type — and must not read as the scanner saying yes
    findings = found(tmp_path, [row(value=None, value_type="null", scan_error="boom")])

    assert findings == []


def test_a_row_naming_a_log_the_run_no_longer_owns_is_dropped(
    tmp_path: Path,
) -> None:
    # an orphan's rows are still in the directory; opening a window over them
    # would be a question nothing can ever resolve
    findings = scan_findings(
        scan_dir_with(tmp_path, [row(uri="/logs/some_orphan.eval")]),
        scanners=SCANNERS,
        attempts=attempts(),
    )

    assert findings.instances == []


def test_the_join_survives_the_two_spellings_of_one_path(tmp_path: Path) -> None:
    # the worker records `absolute_file_path(...)` from inside the eval while
    # observation gets its location from the listing, and on a machine whose
    # temporary directory is a symlink the two disagree by a prefix
    findings = scan_findings(
        scan_dir_with(tmp_path, [row(uri="/private/logs/x_cybench_aaa.eval")]),
        scanners=SCANNERS,
        attempts=attempts("/logs/x_cybench_aaa.eval"),
    )

    assert len(findings.instances) == 1


def test_a_scanner_with_no_parquet_is_absent_rather_than_an_error(
    tmp_path: Path,
) -> None:
    # `quiet` is in the merge and has recorded nothing yet, which is the
    # ordinary shape early in a run
    assert found(tmp_path, [row()])


def test_a_parquet_this_version_cannot_project_costs_only_its_scanner(
    tmp_path: Path,
) -> None:
    # and is *reported*, because a file nobody could open is not a scanner
    # that found nothing — signing over the difference is what this prevents
    scan_dir = scan_dir_with(tmp_path, [row()])
    (Path(scan_dir) / "quiet.parquet").write_bytes(b"not a parquet at all")

    findings = scan_findings(scan_dir, scanners=SCANNERS, attempts=attempts())

    assert len(findings.instances) == 1
    assert [entry.what for entry in findings.unreadable] == ["quiet's scan results"]
    assert findings.unreadable[0].location.endswith("quiet.parquet")


def test_a_findings_ref_is_the_same_string_the_log_read_composes(
    tmp_path: Path,
) -> None:
    """The pin: two composers, one format, and nothing but this test between them.

    `_evalset.instances._instance` builds a ref from a log's sample summary; this module builds one from a scan row. They are joined by convention alone — a window absorbing both would otherwise see one sample as two, and a ruling would cover neither.
    """
    from inspect_steward._evalset.instances import Instance

    findings = found(tmp_path, [row(uuid="uuid-9", sample="17", epoch=3)])

    # exactly the expression `_instance` uses, over the same four values
    from_log = Instance(
        class_key="error:x",
        ref=f"{'eval-1'}:{'17'}:{3}:{'uuid-9'}",
    )
    assert findings[0].ref == from_log.ref
    assert (
        findings[0].eval_id,
        findings[0].sample_id,
        findings[0].epoch,
        findings[0].uuid,
    ) == ("eval-1", "17", 3, "uuid-9")
