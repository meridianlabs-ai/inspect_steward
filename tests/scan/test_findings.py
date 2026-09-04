"""Reading a scan row as an anomaly instance.

A synthesized compacted parquet is the whole fixture, on `test_summary.py`'s grounds exactly: the contract is *this shape of row becomes this instance*, so the rows are stated and the instances asserted. What a real scanner puts in them is `test_bracket.py`'s and the live test's business.

The one thing here that is not about this module is `test_a_findings_ref_is_the_same_string_the_log_read_composes`: two independent composers agree on `Instance.ref` by convention and by nothing else, and a drift between them would silently give one sample two identities — a window that never dedupes and a ruling that covers nothing.
"""

import io
import json
import traceback
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
    "scan_error_traceback",
)
"""Every finding column but the epoch, which is typed and so built separately."""

LOG = "/logs/2026-08-31T10-00-00_cybench_aaa.eval"


def thrown(exception: Exception) -> str:
    """A real `traceback.format_exc()`, which is the only error column that classes.

    Raised and caught rather than hand-written: `scan_error_class` parses what Python actually prints, and a plausible-looking string is how this test would stop testing anything. Every traceback composed here raises in *this* frame, so the exception type is what tells two of them apart.
    """
    try:
        raise exception
    except Exception:
        return traceback.format_exc()


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
    scan_error_traceback: str | None = None,
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
        "scan_error_traceback": scan_error_traceback,
    }


def errored(exception: Exception | None = None, **fields: Any) -> dict[str, Any]:
    """The shape a scanner that threw leaves behind: no verdict, and the exception in its place."""
    raised = (
        exception if exception is not None else TimeoutError("the grader timed out")
    )
    return row(
        value=None,
        value_type="null",
        label=None,
        explanation="",
        scan_error=str(raised),
        scan_error_traceback=thrown(raised),
        **fields,
    )


def scan_dir_with(tmp_path: Path, rows: list[dict[str, Any]]) -> str:
    """A scan directory holding one scanner's compacted parquet."""
    scan_dir = tmp_path / "scans" / "scan_id=run-1"
    scan_dir.mkdir(parents=True)
    write_parquet(scan_dir, "scoring_integrity", rows)
    return str(scan_dir)


def write_parquet(scan_dir: Path, scanner: str, rows: list[dict[str, Any]]) -> None:
    """One scanner's compacted parquet, as the fold leaves it.

    The `input` column carries transcript-history heft, exactly as a real one does, so a projection that stopped being a projection would show up as a test that got slow rather than as nothing at all.
    """
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
    (scan_dir / f"{scanner}.parquet").write_bytes(buffer.getvalue())


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


class TestAScannerThatThrew:
    """The second signature off the same projection: what nothing could read.

    A transcript a scanner errored on is not a transcript that came back clean, and the two are indistinguishable in the findings — so a run whose scans all threw would otherwise be signed as *nothing was flagged*. It becomes its own class, keyed on the scanner and the exception, and the population it makes is the whole distinction: *scanning is broken* spans five hundred transcripts, *this transcript breaks the scanner* is a class of one.
    """

    def test_it_classes_on_the_exception_type_and_the_raising_frame(
        self, tmp_path: Path
    ) -> None:
        findings = found(tmp_path, [errored()])

        assert len(findings) == 1
        assert findings[0].class_key.startswith(
            "scanerror:scoring_integrity:TimeoutError@"
        )
        assert findings[0].class_key.endswith(":thrown")
        assert findings[0].kind == "scanerror"

    def test_it_is_never_also_a_finding(self, tmp_path: Path) -> None:
        # the row carries no verdict, and reading one out of it would be the
        # scanner saying yes about a transcript it never read
        findings = found(tmp_path, [errored()])

        assert not any(entry.kind == "scan" for entry in findings)

    def test_two_exceptions_from_one_scanner_are_two_classes(
        self, tmp_path: Path
    ) -> None:
        # a provider timeout and a scanner bug are two decisions, and merging
        # them would put one ruling in front of somebody for both
        findings = found(
            tmp_path,
            [
                errored(TimeoutError("the grader timed out"), uuid="u1"),
                errored(ValueError("no scores on this sample"), uuid="u2"),
            ],
        )

        assert len({entry.class_key for entry in findings}) == 2

    def test_one_exception_from_two_scanners_is_two_classes(
        self, tmp_path: Path
    ) -> None:
        # the scanner is what an operator decides about — *this scanner did not
        # read these transcripts* — so one flaky provider under two scanners is
        # two holes, not one
        scan_dir = scan_dir_with(tmp_path, [errored()])
        write_parquet(Path(scan_dir), "quiet", [errored()])

        findings = scan_findings(
            scan_dir, scanners=SCANNERS, attempts=attempts()
        ).instances

        scanners = {entry.class_key.split(":")[1] for entry in findings}
        assert scanners == {"scoring_integrity", "quiet"}
        assert len({entry.class_key for entry in findings}) == 2

    def test_a_traceback_nothing_parses_falls_back_to_the_bare_scanner(
        self, tmp_path: Path
    ) -> None:
        # over-merged rather than split into junk, which is this module's
        # doctrine for every other class too
        findings = found(
            tmp_path,
            [
                row(
                    value=None,
                    value_type="null",
                    label=None,
                    scan_error="boom",
                    scan_error_traceback=None,
                )
            ],
        )

        assert [entry.class_key for entry in findings] == [
            "scanerror:scoring_integrity"
        ]

    def test_the_error_text_travels_as_evidence(self, tmp_path: Path) -> None:
        findings = found(tmp_path, [errored()])

        assert findings[0].message == "the grader timed out"

    def test_it_carries_the_transcripts_own_identity(self, tmp_path: Path) -> None:
        # the same ref a finding on that sample would carry, because it *is*
        # that sample — a window over both must see one thing, not two
        findings = found(tmp_path, [errored(uuid="uuid-9", sample="17", epoch=3)])

        assert findings[0].ref == "eval-1:17:3:uuid-9"
        assert findings[0].task == "cybench@openai/gpt-5"


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


class TestWhatEveryScannerAnsweredFor:
    """Coverage's numerator, and the one way it can lie.

    A transcript counts as recorded once **every** configured scanner has answered for it — upstream's own resume predicate. The failure worth a class of its own is the scanner that produced no file at all: dropping it from the intersection lets the scanners that did run report the run as fully scanned on its behalf, which is exactly the shape of the two things coverage exists to show (a scanner added at a re-launch, and one that never started).
    """

    def test_a_transcript_both_scanners_answered_for_is_recorded(
        self, tmp_path: Path
    ) -> None:
        scan_dir = scan_dir_with(tmp_path, [row(uuid="u1"), row(uuid="u2")])
        write_parquet(Path(scan_dir), "quiet", [row(uuid="u1"), row(uuid="u2")])

        found = scan_findings(scan_dir, scanners=SCANNERS, attempts=attempts())

        assert found.recorded == {"cybench@openai/gpt-5": {"u1", "u2"}}

    def test_a_transcript_only_one_scanner_reached_is_not(self, tmp_path: Path) -> None:
        # the third scanner will be sent back for it, so it is not covered yet
        scan_dir = scan_dir_with(tmp_path, [row(uuid="u1"), row(uuid="u2")])
        write_parquet(Path(scan_dir), "quiet", [row(uuid="u1")])

        found = scan_findings(scan_dir, scanners=SCANNERS, attempts=attempts())

        assert found.recorded == {"cybench@openai/gpt-5": {"u1"}}

    def test_a_scanner_that_produced_no_file_covers_nothing(
        self, tmp_path: Path
    ) -> None:
        # the defect this class is named for: `quiet` is in the committed merge
        # and has no parquet, so it has answered for nothing — and one scanner
        # that answered for everything must not report the run as covered
        found = scan_findings(
            scan_dir_with(tmp_path, [row(uuid="u1"), row(uuid="u2")]),
            scanners=SCANNERS,
            attempts=attempts(),
        )

        assert found.recorded == {"cybench@openai/gpt-5": set()}

    def test_a_parquet_that_will_not_read_covers_nothing_either(
        self, tmp_path: Path
    ) -> None:
        # the stronger version of the same: what that file said is unknown, so
        # nothing in it can be counted as answered
        scan_dir = scan_dir_with(tmp_path, [row(uuid="u1")])
        (Path(scan_dir) / "quiet.parquet").write_bytes(b"not a parquet at all")

        found = scan_findings(scan_dir, scanners=SCANNERS, attempts=attempts())

        assert found.recorded == {"cybench@openai/gpt-5": set()}
        assert len(found.unreadable) == 1

    def test_an_errored_row_still_counts_as_recorded(self, tmp_path: Path) -> None:
        # coverage is recorded rows against landed samples, and a scanner that
        # threw is its own class — counting it here too would report one
        # failure twice in two places that disagree about what to do with it
        scan_dir = scan_dir_with(tmp_path, [errored(uuid="u1")])
        write_parquet(Path(scan_dir), "quiet", [row(uuid="u1")])

        found = scan_findings(scan_dir, scanners=SCANNERS, attempts=attempts())

        assert found.recorded == {"cybench@openai/gpt-5": {"u1"}}


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
