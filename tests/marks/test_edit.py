"""Writing a ruling into a landed log: the primitives, on synthesized logs.

The file is the effect, so every claim here is read back off disk after `commit`: the unscored value and its reason, the score's history with the ruling's provenance, the metrics recomputed over what remains, and the samples nobody ruled on byte-identical. Inspect makes the edit (`exclude_samples`, `zero_samples`); what is Steward's is the ruling in the provenance, finding a ruling already written, and deferring a sample that moved.
"""

import math
from dataclasses import replace
from pathlib import Path

import inspect_ai.log
import pytest
from inspect_ai.log import (
    EvalLog,
    EvalSample,
    ProvenanceData,
    SampleSelector,
    read_eval_log,
)
from inspect_ai.scorer import Score
from inspect_steward._anomaly.model import (
    Anomaly,
    AnomalyState,
    Disposition,
    Evidence,
    Ruling,
)
from inspect_steward._marks import edit
from inspect_steward._marks.edit import (
    STEWARD,
    Target,
    commit,
    exclude_targets,
    marked_by,
    zero_targets,
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


@pytest.fixture
def inspect_6631(monkeypatch: pytest.MonkeyPatch) -> list[list[SampleSelector]]:
    """Inspect's `exclude_samples` and `zero_samples` returning `None`, as they do from UKGovernmentBEIS/inspect_ai#6631, so the outcome is read from the log. Returns the samples each call selected."""
    calls: list[list[SampleSelector]] = []

    def exclude(
        log: EvalLog, samples: list[SampleSelector], provenance: ProvenanceData
    ) -> None:
        calls.append(list(samples))
        inspect_ai.log.exclude_samples(log, samples, provenance)

    def zero(
        log: EvalLog,
        empty: EvalLog,
        samples: list[SampleSelector],
        provenance: ProvenanceData,
    ) -> None:
        calls.append(list(samples))
        inspect_ai.log.zero_samples(log, empty, samples, provenance)

    monkeypatch.setattr(edit, "exclude_samples", exclude)
    monkeypatch.setattr(edit, "zero_samples", zero)
    return calls


class TestExclusion:
    @pytest.mark.usefixtures("inspect_6631")
    def test_every_score_becomes_unscored_with_the_reason_and_provenance(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)

        marked = exclude_targets(
            log, [target(log, "s2"), target(log, "s3")], anomaly, ruling
        )
        commit(log, location)

        assert [one.sample_id for one in marked.edited] == ["s2", "s3"]
        assert marked.scores == {"exact"}
        assert marked.deferred == [] and marked.found == []
        after = read_eval_log(location)
        excluded = sample(after, "s2").scores or {}
        assert math.isnan(float(excluded["exact"].as_float()))
        assert excluded["exact"].reason == "excluded"
        assert excluded["exact"].explanation == "the provider was down"
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

        exclude_targets(log, [target(log, "s3")], anomaly, ruling)
        commit(log, location)

        scores = sample(read_eval_log(location), "s3").scores or {}
        assert list(scores) == ["exact"]
        assert scores["exact"].reason == "excluded"
        assert math.isnan(scores["exact"].as_float())

    def test_the_metrics_are_recomputed_over_the_rest(self, tmp_path: Path) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)
        assert accuracy(log) == 0.5, "the premise: the header said one of two"
        untouched = sample(log, "s1").model_dump()

        exclude_targets(log, [target(log, "s2")], anomaly, ruling)
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
        exclude_targets(log, [target(log, "s2")], anomaly, ruling)
        commit(log, location)

        again = read_eval_log(location)
        marked = exclude_targets(again, [target(again, "s2")], anomaly, ruling)

        assert marked.edited == [] and marked.scores == set()
        assert [one.sample_id for one in marked.found] == ["s2"]
        assert len((sample(again, "s2").scores or {})["exact"].history) == 2
        assert marked_by(sample(again, "s2"), RULED_AT)
        assert not marked_by(sample(again, "s1"), RULED_AT)

    def test_a_sample_that_moved_is_deferred(self, tmp_path: Path) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)

        marked = exclude_targets(
            log, [target(log, "s2", uuid="uuid-s2-1-2")], anomaly, ruling
        )

        assert marked.edited == []
        ((deferred, why),) = marked.deferred
        assert deferred.sample_id == "s2" and "uuid" in why
        assert (sample(log, "s2").scores or {})["exact"].history == []

    def test_a_sample_another_ruling_excluded_is_found_and_left_out_of_the_call(
        self, tmp_path: Path, inspect_6631: list[list[SampleSelector]]
    ) -> None:
        """Inspect excludes an excluded sample again if asked, so it is not asked."""
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)
        exclude_targets(log, [target(log, "s2")], anomaly, ruling)
        commit(log, location)
        inspect_6631.clear()

        again = read_eval_log(location)
        later = replace(ruling, ts="2026-09-01T00:00:00Z")
        marked = exclude_targets(
            again, [target(again, "s2"), target(again, "s1")], anomaly, later
        )

        assert [one.sample_id for one in marked.found] == ["s2"]
        assert [one.sample_id for one in marked.edited] == ["s1"]
        assert inspect_6631 == [[("s1", 1)]]
        assert len((sample(again, "s2").scores or {})["exact"].history) == 2

    def test_inspect_returning_without_an_edit_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)

        def no_edit(
            log: EvalLog, samples: list[SampleSelector], provenance: ProvenanceData
        ) -> None:
            return None

        monkeypatch.setattr(edit, "exclude_samples", no_edit)
        with pytest.raises(RuntimeError, match="without editing s2 \\(epoch 1\\)"):
            exclude_targets(log, [target(log, "s2")], anomaly, ruling)

    def test_a_log_naming_no_scorer_refuses_and_edits_nothing(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path, scorers=[])
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.EXCLUDE)

        with pytest.raises(ValueError, match="names no scorer"):
            exclude_targets(
                log, [target(log, "s2"), target(log, "s3")], anomaly, ruling
            )

        assert (sample(log, "s2").scores or {})["exact"].history == []


def empty_attempt(log: EvalLog, verdicts: dict[str, Score | None]) -> EvalLog:
    """The side run's log of the same task: run with `empty_attempt()`, each sample scored as `verdicts` says."""
    samples = [
        EvalSample(
            id=sample_id,
            epoch=1,
            input="question",
            target="answer",
            uuid=f"scratch-{sample_id}",
            scores={"exact": verdict} if verdict is not None else None,
        )
        for sample_id, verdict in verdicts.items()
    ]
    return log.model_copy(
        update={
            "eval": log.eval.model_copy(update={"solver": "inspect_ai/empty_attempt"}),
            "status": "success",
            "samples": samples,
        }
    )


EMPTY = Score(
    value="I", answer="", explanation="empty attempt", metadata={"grader": "exact"}
)


class TestZero:
    @pytest.mark.usefixtures("inspect_6631")
    def test_the_empty_attempts_verdict_is_copied_in_with_its_history(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.ZERO)
        empty = empty_attempt(read_eval_log(location), {"s1": EMPTY})
        events_before = list(sample(log, "s1").events)

        marked = zero_targets(log, empty, [target(log, "s1")], anomaly, ruling)
        commit(log, location)

        assert [one.sample_id for one in marked.edited] == ["s1"]
        assert marked.scores == {"exact"}
        after = read_eval_log(location)
        zeroed = (sample(after, "s1").scores or {})["exact"]
        assert zeroed.value == "I"
        assert zeroed.answer == "" and zeroed.explanation == "empty attempt"
        assert zeroed.metadata == {"grader": "exact"}
        assert zeroed.reason == "zeroed"
        assert zeroed.history[0].value == 1.0
        assert zeroed.history[1].provenance is not None
        assert zeroed.history[1].provenance.metadata[STEWARD]["disposition"] == "zero"
        # the transcript is untouched: the one new event is the edit itself
        events = sample(after, "s1").events
        assert events[:-1] == events_before and events[-1].event == "score_edit"
        assert accuracy(after) == 0.0

    def test_a_second_pass_finds_the_mark_and_edits_nothing(
        self, tmp_path: Path
    ) -> None:
        """Inspect zeroes again when asked, so the record that a ruling is written is Steward's."""
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.ZERO)
        empty = empty_attempt(read_eval_log(location), {"s1": EMPTY})
        zero_targets(log, empty, [target(log, "s1")], anomaly, ruling)
        commit(log, location)

        again = read_eval_log(location)
        marked = zero_targets(again, empty, [target(again, "s1")], anomaly, ruling)

        assert marked.edited == [] and [one.sample_id for one in marked.found] == ["s1"]
        assert len((sample(again, "s1").scores or {})["exact"].history) == 2

    def test_an_empty_attempt_without_a_verdict_refuses_and_edits_nothing(
        self, tmp_path: Path
    ) -> None:
        location = landed(tmp_path)
        log = read_eval_log(location)
        anomaly, ruling = decision(Disposition.ZERO)
        empty = empty_attempt(read_eval_log(location), {"s1": EMPTY, "s2": None})

        with pytest.raises(ValueError, match="no verdict"):
            zero_targets(
                log, empty, [target(log, "s1"), target(log, "s2")], anomaly, ruling
            )

        assert (sample(log, "s1").scores or {})["exact"].history == []
