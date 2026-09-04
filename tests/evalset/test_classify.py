"""Class keys that group what is the same and split what is not.

The property that matters is stability: the same failure on different samples, tasks, hosts, and line numbers must produce byte-identical keys, because the key is what turns five hundred errors into one decision. The second property is what the key excludes — message text, absolute paths, line numbers — because any of those in the key silently splits one population into many.

Everything here is pure text in, string out; no files, no processes.
"""

import pytest
from inspect_steward._evalset.classify import (
    OPERATOR_LIMIT,
    UNCLASSED,
    VANISHED,
    ParsedError,
    cancelled,
    digest8,
    error_class,
    kind_of,
    no_log_class,
    parse_error,
    scan_class,
    scan_family,
    scan_task,
    substrate,
    task_error_class,
    zero_class,
)

TIMEOUT_TRACEBACK = """Traceback (most recent call last):
  File "/home/kaia/.venv/lib/python3.13/site-packages/inspect_ai/solver/_basic_agent.py", line 142, in solve
    output = await get_model().generate(input)
  File "/home/kaia/.venv/lib/python3.13/site-packages/openai/_client.py", line 88, in post
    raise APITimeoutError(request=request)
openai.APITimeoutError: Request timed out.
"""

CHAINED_TRACEBACK = """Traceback (most recent call last):
  File "/work/evals/scorer.py", line 12, in score
    parsed = json.loads(completion)
  File "/usr/lib/python3.13/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/work/evals/scorer.py", line 15, in score
    raise ScorerError(f"unparseable: {completion[:50]}")
evals.scorer.ScorerError: unparseable: I think the answer
"""

GROUP_TRACEBACK = """  + Exception Group Traceback (most recent call last):
  |   File "/work/run.py", line 3, in <module>
  |     raise ExceptionGroup("many", [ValueError("a")])
  | ExceptionGroup: many (1 sub-exception)
  +-+---------------- 1 ----------------
    | ValueError: a
    +------------------
"""


class TestParseError:
    def test_type_and_deepest_frame_from_a_standard_traceback(self) -> None:
        parsed = parse_error("APITimeoutError(...)", TIMEOUT_TRACEBACK)

        assert parsed == ParsedError(
            type="openai.APITimeoutError",
            frame="openai/_client.py:post",
            path="/home/kaia/.venv/lib/python3.13/site-packages/openai/_client.py",
        )

    def test_the_final_block_of_a_chained_traceback_is_the_identity(self) -> None:
        # the outermost exception is the one that halted the sample; the
        # original cause is context, not identity
        parsed = parse_error(None, CHAINED_TRACEBACK)

        assert parsed is not None
        assert parsed.type == "evals.scorer.ScorerError"
        assert parsed.frame == "evals/scorer.py:score"

    def test_a_bare_exception_line_with_no_message_still_matches(self) -> None:
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "/work/tool.py", line 9, in run\n'
            "    await proc.wait()\n"
            "KeyboardInterrupt\n"
        )

        parsed = parse_error(None, traceback)

        assert parsed is not None
        assert parsed.type == "KeyboardInterrupt"

    def test_a_repr_message_yields_the_type_with_no_frame(self) -> None:
        parsed = parse_error("TimeoutError('too slow on sample 41')", None)

        assert parsed == ParsedError(type="TimeoutError", frame="unknown")

    def test_prose_with_no_traceback_parses_to_nothing(self) -> None:
        assert parse_error("something went wrong", None) is None
        assert parse_error(None, None) is None

    def test_an_exception_group_falls_back_to_the_message(self) -> None:
        # every line of a group traceback is indented or piped, so nothing
        # matches at column 0 — the repr fallback is what catches it
        parsed = parse_error(
            "ExceptionGroup('many', [ValueError('a')])", GROUP_TRACEBACK
        )

        assert parsed == ParsedError(type="ExceptionGroup", frame="unknown")


class TestStableKeys:
    def test_line_numbers_are_not_in_the_key(self) -> None:
        # an edited-and-relaunched definition shifts every line; precedent
        # must survive an edit that did not touch the failing code path
        shifted = TIMEOUT_TRACEBACK.replace("line 88", "line 91").replace(
            "line 142", "line 150"
        )

        assert error_class(None, shifted) == error_class(None, TIMEOUT_TRACEBACK)

    def test_absolute_paths_are_not_in_the_key(self) -> None:
        elsewhere = TIMEOUT_TRACEBACK.replace("/home/kaia/.venv", "/opt/runner/env")

        key = error_class(None, TIMEOUT_TRACEBACK)
        assert error_class(None, elsewhere) == key
        assert key == "error:openai.APITimeoutError@openai/_client.py:post"

    def test_message_text_is_never_in_the_key(self) -> None:
        first = error_class("TimeoutError('sample 41 at 02:14')", None)
        second = error_class("TimeoutError('sample 407 at 05:52')", None)

        assert first == second == "error:TimeoutError@unknown"

    def test_nothing_parseable_is_the_unclassed_bucket(self) -> None:
        assert error_class("it broke", None) == UNCLASSED
        assert error_class(None, None) == UNCLASSED


class TestTaskClasses:
    def test_a_task_error_classes_on_the_header_error(self) -> None:
        assert (
            task_error_class(None, TIMEOUT_TRACEBACK)
            == "task:error:openai.APITimeoutError@openai/_client.py:post"
        )
        assert task_error_class("nothing useful", None) == "task:error"

    def test_a_departed_worker_classes_on_its_tail(self) -> None:
        tail = (
            "Traceback (most recent call last):\n"
            '  File "/work/evalset.py", line 3, in <module>\n'
            "    from missing_lib import thing\n"
            "ModuleNotFoundError: No module named 'missing_lib'\n"
        )

        assert (
            no_log_class(tail)
            == "task:no-log-exit:ModuleNotFoundError@work/evalset.py:<module>"
        )
        assert no_log_class("") == "task:no-log"
        assert no_log_class("error: could not resolve model") == "task:no-log"


class TestScoreAndKind:
    def test_zero_class_is_per_task_and_filename_safe(self) -> None:
        key = zero_class("swe bench (hard)", "identifier-a")

        assert key == f"score:zero:swe-bench-hard:{digest8('identifier-a')}"
        assert zero_class("swe bench (hard)", "identifier-b") != key

    def test_scan_class_is_per_label_and_per_task(self) -> None:
        key = scan_class(
            "scoring_integrity",
            "reward_hacking",
            task="cybench",
            identifier="cybench@openai/gpt-5",
        )

        assert key == (
            "scan:scoring_integrity:reward_hacking:cybench:"
            f"{digest8('cybench@openai/gpt-5')}"
        )
        # the same scanner reporting a different category is a different
        # decision, and a label is the only thing that says so
        assert key != scan_class(
            "scoring_integrity",
            "refusal",
            task="cybench",
            identifier="cybench@openai/gpt-5",
        )
        # and the same finding in another task, or in this task under another
        # model, is decided on its own when that task lands
        assert key != scan_class(
            "scoring_integrity",
            "reward_hacking",
            task="gaia",
            identifier="gaia@openai/gpt-5",
        )
        assert key != scan_class(
            "scoring_integrity",
            "reward_hacking",
            task="cybench",
            identifier="cybench@anthropic/claude-opus-5",
        )

    def test_a_family_is_the_finding_without_its_task(self) -> None:
        # what one task's ruling is precedent for in the next
        first = scan_class(
            "integrity", "reward_hacking", task="cybench", identifier="a"
        )
        second = scan_class("integrity", "reward_hacking", task="gaia", identifier="b")

        assert (
            scan_family(first) == scan_family(second) == "scan:integrity:reward_hacking"
        )
        assert (scan_task(first), scan_task(second)) == ("cybench", "gaia")
        bare = scan_class("integrity", None, task="cybench", identifier="a")
        assert scan_family(bare) == "scan:integrity"
        # other kinds, and a scan key too short to carry a task, are their own
        assert scan_family(OPERATOR_LIMIT) == OPERATOR_LIMIT
        assert scan_family("scan:integrity") == "scan:integrity"
        assert scan_task("scan:integrity") == ""

    def test_a_scanner_with_no_label_classes_on_its_name_alone(self) -> None:
        bare = scan_class("integrity", None, task="cybench", identifier="a")

        assert bare == f"scan:integrity:cybench:{digest8('a')}"
        assert scan_class("integrity", "", task="cybench", identifier="a") == bare

    def test_sanitizing_a_segment_does_not_merge_two_of_them(self) -> None:
        # `unsafe output` and `unsafe-output` are two labels a scanner can
        # plausibly emit, and one ruling settling both would close findings
        # the operator who ruled never saw
        spaced = scan_class("integrity", "unsafe output", task="t", identifier="a")
        hyphenated = scan_class("integrity", "unsafe-output", task="t", identifier="a")

        assert spaced != hyphenated
        # the readable form still leads, so a prefix still selects it
        assert spaced.startswith("scan:integrity:unsafe-output")
        # and the one that needed no repair carries no digest of its own
        assert hyphenated == f"scan:integrity:unsafe-output:t:{digest8('a')}"

    @pytest.mark.parametrize(
        ("key", "kind"),
        [
            ("error:TimeoutError@unknown", "error"),
            (OPERATOR_LIMIT, "limit"),
            (VANISHED, "task"),
            ("task:no-log", "task"),
            ("score:zero:probe:abcd1234", "score"),
            ("scan:scoring_integrity:reward_hacking", "scan"),
            ("scan:bare", "scan"),
        ],
    )
    def test_kind_is_the_first_segment(self, key: str, kind: str) -> None:
        assert kind_of(key) == kind


class TestCancellation:
    def test_a_cancellation_repr_is_teardown_not_an_instance(self) -> None:
        assert cancelled("CancelledError()")
        assert not cancelled("TimeoutError('too slow')")
        assert not cancelled(None)


class TestSubstrate:
    def test_a_storage_stack_frame_is_substrate_whatever_the_type(self) -> None:
        parsed = ParsedError(
            type="FileNotFoundError",
            frame="s3fs/core.py:_fetch",
            path="/venv/lib/python3.13/site-packages/s3fs/core.py",
        )

        assert substrate(parsed, None)

    def test_a_credential_type_is_substrate_wherever_it_raised(self) -> None:
        parsed = ParsedError(
            type="botocore.exceptions.NoCredentialsError",
            frame="evals/setup.py:connect",
            path="/work/evals/setup.py",
        )

        assert substrate(parsed, None)

    def test_file_not_found_in_user_code_is_not_substrate(self) -> None:
        # an OSError subclass, and usually a dataset bug — the one the
        # final-segment exact match deliberately keeps out
        parsed = ParsedError(
            type="FileNotFoundError",
            frame="evals/dataset.py:load",
            path="/work/evals/dataset.py",
        )

        assert not substrate(parsed, "FileNotFoundError(2, 'No such file')")

    def test_a_disk_full_message_is_substrate_with_no_parse_at_all(self) -> None:
        assert substrate(None, "OSError: [Errno 28] No space left on device")

    def test_an_ordinary_failure_is_not_substrate(self) -> None:
        parsed = ParsedError(
            type="ValueError",
            frame="evals/scorer.py:score",
            path="/work/evals/scorer.py",
        )

        assert not substrate(parsed, "ValueError('bad completion')")
