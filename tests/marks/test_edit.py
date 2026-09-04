"""Writing a ruling into a landed log: the primitives, on synthesized logs.

The file is the effect, so every claim here is read back off disk after `commit`: the unscored value and its reason, the score's history with the ruling's provenance, the metrics recomputed over what remains, and the samples nobody ruled on byte-identical.
"""

import math
from pathlib import Path

from inspect_ai.log import EvalLog, EvalSample, read_eval_log
from inspect_ai.scorer import Score
from inspect_steward._anomaly.model import (
    Anomaly,
    AnomalyState,
    Disposition,
    Evidence,
    Ruling,
)
from inspect_steward._marks.edit import (
    EXCLUDED,
    STEWARD,
    ZEROED,
    Target,
    commit,
    harvest_scores,
    mark_unscored,
    marked_by,
)

from .._logs import SynthSample, SynthTask, write_log

CLASS = "error:openai.APITimeoutError@openai/_client.py:post"
RULED_AT = "2026-08-31T00:00:00Z"
PROBE = SynthTask("probe", samples=3)


def decision(disposition: Disposition) -> tuple[Anomaly, Ruling]:
    ruling = Ruling(
        class_key=CLASS,
        disposition=disposition,
        reason="the provider was down",
        by="kaia",
        ts=RULED_AT,
    )
    return (
        Anomaly(
            class_key=CLASS,
            kind="error",
            state=AnomalyState.ACCEPTED,
            evidence=Evidence(count=2),
            generation=2,
            ruling=ruling,
        ),
        ruling,
    )


def landed(tmp_path: Path, *, scorers: list[str] | None = None) -> str:
    """One scored sample, one scored low, one errored and never scored."""
    return str(
        write_log(
            tmp_path,
            PROBE,
            completed=2,
            scores={"exact": {"accuracy": 0.5}},
            scorers=["exact"] if scorers is None else scorers,
            samples=[
                SynthSample("s1", score=1.0),
                SynthSample("s2", score=0.0),
                SynthSample("s3", error="APITimeoutError('s3')"),
            ],
        )
    )


def target(log: EvalLog, sample_id: str, *, uuid: str | None = None) -> Target:
    return Target(
        task=PROBE.identifier,
        location=log.location,
        eval_id=log.eval.eval_id,
        sample_id=sample_id,
        epoch=1,
        uuid=uuid if uuid is not None else f"uuid-{sample_id}-1",
    )


def sample(log: EvalLog, sample_id: str) -> EvalSample:
    return next(one for one in log.samples or [] if one.id == sample_id)


def accuracy(log: EvalLog) -> float:
    assert log.results is not None
    return log.results.scores[0].metrics["accuracy"].value


class TestExclusion:
    def test_every_score_becomes_unscored_with_the_reason_and_provenance(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)

        marked = mark_unscored(
            log, [target(log, "s2"), target(log, "s3")], anomaly, ruling
        )
        commit(log, location)

        assert [one.sample_id for one in marked.edited] == ["s2", "s3"]
        assert marked.scores == {"exact"}
        assert marked.deferred == [] and marked.found == []
        after = read_eval_log(location)
        excluded = sample(after, "s2").scores or {}
        assert math.isnan(float(excluded["exact"].as_float()))
        assert excluded["exact"].reason == EXCLUDED
        assert "excluded by kaia" in (excluded["exact"].explanation or "")
        # the pre-edit score is the first history entry; the edit, with the
        # ruling's provenance, the second
        first, second = excluded["exact"].history
        assert first.value == 0.0 and first.provenance is None
        assert second.provenance is not None
        assert second.provenance.author == "kaia"
        assert second.provenance.reason == "the provider was down"
        assert second.provenance.metadata[STEWARD] == {
            "class": CLASS,
            "generation": 2,
            "ruling": RULED_AT,
            "disposition": "exclude",
        }
        assert [event.event for event in sample(after, "s2").events] == ["score_edit"]

    def test_an_errored_sample_gets_a_score_named_after_the_tasks_scorer(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)

        mark_unscored(log, [target(log, "s3")], anomaly, ruling)
        commit(log, location)

        scores = sample(read_eval_log(location), "s3").scores or {}
        assert list(scores) == ["exact"]
        assert scores["exact"].reason == EXCLUDED
        assert math.isnan(scores["exact"].as_float())

    def test_the_metrics_are_recomputed_over_the_rest(self, tmp_path: Path) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)
        assert accuracy(log) == 0.5, "the premise: the header said one of two"
        untouched = sample(log, "s1").model_dump()

        mark_unscored(log, [target(log, "s2")], anomaly, ruling)
        commit(log, location)

        after = read_eval_log(location)
        assert accuracy(after) == 1.0
        assert sample(after, "s1").model_dump() == untouched

    def test_a_second_pass_finds_the_mark_and_edits_nothing(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)
        mark_unscored(log, [target(log, "s2")], anomaly, ruling)
        commit(log, location)

        again = read_eval_log(location)
        marked = mark_unscored(again, [target(again, "s2")], anomaly, ruling)

        assert marked.edited == [] and marked.scores == set()
        assert [one.sample_id for one in marked.found] == ["s2"]
        assert len((sample(again, "s2").scores or {})["exact"].history) == 2
        assert marked_by(sample(again, "s2"), RULED_AT)
        assert not marked_by(sample(again, "s1"), RULED_AT)

    def test_a_sample_that_moved_is_deferred(self, tmp_path: Path) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)

        marked = mark_unscored(
            log, [target(log, "s2", uuid="uuid-s2-1-2")], anomaly, ruling
        )

        assert marked.edited == []
        ((deferred, why),) = marked.deferred
        assert deferred.sample_id == "s2" and "uuid" in why
        assert (sample(log, "s2").scores or {})["exact"].history == []

    def test_a_log_naming_no_scorer_has_nowhere_to_record_it(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path, scorers=[])
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)

        marked = mark_unscored(log, [target(log, "s3")], anomaly, ruling)

        ((deferred, why),) = marked.deferred
        assert deferred.sample_id == "s3" and "scorer" in why


class TestHarvest:
    def test_the_side_runs_verdict_is_copied_in_with_its_history(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.ZERO)
        side = EvalSample(
            id="s1",
            epoch=1,
            input="question",
            target="answer",
            uuid="scratch",
            scores={
                "exact": Score(
                    value="I",
                    answer="",
                    explanation="empty attempt",
                    metadata={"grader": "exact"},
                )
            },
        )
        events_before = list(sample(log, "s1").events)

        marked = harvest_scores(log, {target(log, "s1"): side}, anomaly, ruling)
        commit(log, location)

        assert [one.sample_id for one in marked.edited] == ["s1"]
        after = read_eval_log(location)
        zeroed = (sample(after, "s1").scores or {})["exact"]
        assert zeroed.value == "I"
        assert zeroed.answer == "" and zeroed.explanation == "empty attempt"
        assert zeroed.metadata == {"grader": "exact"}
        assert zeroed.reason == ZEROED
        assert zeroed.history[0].value == 1.0
        assert zeroed.history[1].provenance is not None
        assert zeroed.history[1].provenance.metadata[STEWARD]["disposition"] == "zero"
        # the transcript is untouched: the one new event is the edit itself
        events = sample(after, "s1").events
        assert events[:-1] == events_before and events[-1].event == "score_edit"
        assert accuracy(after) == 0.0

    def test_a_side_sample_without_a_score_defers_its_target(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.ZERO)
        side = EvalSample(id="s1", epoch=1, input="q", target="a", uuid="scratch")

        marked = harvest_scores(log, {target(log, "s1"): side}, anomaly, ruling)

        assert marked.edited == []
        ((deferred, why),) = marked.deferred
        assert deferred.sample_id == "s1" and "no score" in why
