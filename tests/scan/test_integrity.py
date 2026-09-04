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
from inspect_steward._scan.integrity import integrity_question

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
