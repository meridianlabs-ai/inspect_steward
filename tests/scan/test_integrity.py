"""What the scoring-integrity reviewer is shown about the score it is judging.

The scanner's whole question is whether a recorded score can be trusted. Shown
only the number, the best it can do is say the number looks inconsistent with
the trajectory and recommend that an operator go and read the grader logs — a
finding nobody can act on, raised against evidence sitting on the same sample.

Observed on a SWE-bench-style set: five samples flagged, every explanation
ending in *a reviewer should inspect the grader logs*, and four of the five
resolvable from the grader's output alone. One decisively: the same instance
under another model scored 1.0 on the same grader at 171/171, while the flagged
run failed five named tests.
"""

from inspect_scout import Transcript
from inspect_steward._scan.integrity import EXPLANATION_CHARS, integrity_question

GRADER = (
    "Test exit code: 1 stdout: Running gold tests -- test/audio/VoiceRecording-test.ts "
    "Required tests: 171 Passed tests: 169 Required tests that passed: 166 "
    "RESULT: FAILED Missing tests: ['should record high-quality audio']"
)


def transcript(**fields: object) -> Transcript:
    return Transcript.model_validate({"transcript_id": "t1", **fields})


def test_the_graders_own_output_is_quoted_to_the_reviewer() -> None:
    question = integrity_question(
        transcript(score=0.0, success=False, score_explanation=GRADER)
    )

    assert "Required tests: 171" in question
    assert "Passed tests: 169" in question


def test_and_the_reviewer_is_told_not_to_defer_to_one() -> None:
    # the failure this exists to stop is a finding whose entire content is
    # *somebody should go and look at the thing you were just given*
    question = integrity_question(
        transcript(score=0.0, success=False, score_explanation=GRADER)
    )

    assert "read them yourself" in question


def test_a_scorer_that_explained_nothing_reads_as_it_always_did() -> None:
    question = integrity_question(transcript(score=1.0, success=True))

    assert "RECORDED OUTCOME" in question
    assert "WHAT THE SCORER SAID" not in question


def test_an_empty_explanation_is_not_an_explanation() -> None:
    question = integrity_question(
        transcript(score=0.0, success=False, score_explanation="   ")
    )

    assert "WHAT THE SCORER SAID" not in question


def test_an_unscored_sample_still_says_so() -> None:
    question = integrity_question(transcript())

    assert "not available to you" in question


LOG_DUMP = (
    "Test exit code: 1 stdout: Running gold tests -- test/audio/VoiceRecording-test.ts\n"
    + "PASSED test/audio/VoiceRecording-test.ts::should_record_sample_000123 ... ok\n"
    * 40_000
    + "RESULT: FAILED Missing tests: ['should record high-quality audio']"
)
"""A verifier that wrote its whole test log into the explanation: about three million characters, with the command at the top and the verdict at the bottom."""


def test_a_log_dump_is_shown_by_its_head_and_tail_rather_than_whole() -> None:
    # the question is scaffolding chunking cannot split: four such explanations
    # of 0.8 to 5.2 million characters left no room for a single message and
    # the scanner raised before reading one
    bare = integrity_question(transcript(score=0.0, success=False))
    question = integrity_question(
        transcript(score=0.0, success=False, score_explanation=LOG_DUMP)
    )

    assert len(question) - len(bare) < EXPLANATION_CHARS + 1_000
    # the command that produced the log, and the verdict that settles the question
    assert "Test exit code: 1 stdout: Running gold tests" in question
    assert (
        "RESULT: FAILED Missing tests: ['should record high-quality audio']" in question
    )
    # and the gap is named, so it reads as a gap
    elided = len(LOG_DUMP) - EXPLANATION_CHARS
    assert f"[... {elided:,} characters of scorer output elided ...]" in question
    assert f"ran to {len(LOG_DUMP):,} characters" in question
    assert "verbatim" not in question


def test_an_ordinary_explanation_is_still_quoted_whole() -> None:
    question = integrity_question(
        transcript(score=0.0, success=False, score_explanation=GRADER)
    )

    assert "verbatim" in question
    assert "elided" not in question
