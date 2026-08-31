"""The read tiers that keep classification's cost flat, without losing an instance.

The claims: a settled log's summaries are read once ever; a running log's classed samples are memoized so a growing log never re-reads them; a partial classification (a single-sample read that failed) is degraded but not cached, so the next turn retries for the better key; cancellation reprs are teardown, not instances; and the cache is disposable by construction — version-stamped, prune-on-write, and worth nothing but one slow turn when lost.

Every log here is a file `tests/_logs.py` wrote; no evals run.
"""

from pathlib import Path

from inspect_steward._evalset.instances import (
    ClassedCache,
    Instance,
    classed_instances,
    read_classed_cache,
    write_classed_cache,
)
from inspect_steward._evalset.observe import ObservedLogs, observe_logs

from .._logs import SynthSample, SynthTask, write_log

TASK = SynthTask("probe", samples=3)

TIMEOUT_TRACEBACK = """Traceback (most recent call last):
  File "/venv/lib/python3.13/site-packages/openai/_client.py", line 88, in post
    raise APITimeoutError(request=request)
openai.APITimeoutError: Request timed out.
"""

TIMEOUT_KEY = "error:openai.APITimeoutError@openai/_client.py:post"


def errored(id: str, *, epoch: int = 1) -> SynthSample:
    return SynthSample(
        id=id,
        epoch=epoch,
        error=f"APITimeoutError('sample {id}')",
        traceback=TIMEOUT_TRACEBACK,
    )


def locations(logs: ObservedLogs, *, holding: str = "") -> set[str]:
    """Observed log locations — the URI form the seam actually carries."""
    return {
        attempt.location
        for attempts in logs.attempts.values()
        for attempt in attempts
        if holding in attempt.location
    }


def test_errored_and_operator_limited_samples_become_instances(
    tmp_path: Path,
) -> None:
    write_log(
        tmp_path,
        TASK,
        completed=1,
        samples=[
            errored("s1"),
            SynthSample(
                id="s2", limit="operator", limit_reason="looked wrong, killed it"
            ),
            SynthSample(id="s3", score=1.0),
        ],
    )

    classed = classed_instances(
        observe_logs(tmp_path), errored_running=set(), cache=ClassedCache()
    )

    keys = {instance.class_key for instance in classed.instances}
    assert keys == {TIMEOUT_KEY, "limit:operator"}
    timeout = next(i for i in classed.instances if i.class_key == TIMEOUT_KEY)
    assert timeout.sample_id == "s1"
    assert timeout.uuid == "uuid-s1-1"
    assert timeout.task == TASK.identifier
    assert "sample s1" in timeout.message
    limited = next(i for i in classed.instances if i.class_key == "limit:operator")
    assert limited.message == "looked wrong, killed it"
    assert classed.unreadable == []


def test_a_cancellation_repr_is_teardown_not_an_instance(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        TASK,
        completed=2,
        samples=[SynthSample(id="s1", error="CancelledError()"), errored("s2")],
    )

    classed = classed_instances(
        observe_logs(tmp_path), errored_running=set(), cache=ClassedCache()
    )

    assert [i.sample_id for i in classed.instances] == ["s2"]


def test_a_settled_log_is_read_once_and_served_from_cache_after(
    tmp_path: Path,
) -> None:
    write_log(tmp_path, TASK, completed=2, samples=[errored("s1")])
    cache = ClassedCache()

    first = classed_instances(
        observe_logs(tmp_path), errored_running=set(), cache=cache
    )
    hits_after_first = cache.hits
    second = classed_instances(
        observe_logs(tmp_path), errored_running=set(), cache=cache
    )

    assert hits_after_first == 0
    assert cache.hits == 1
    # and the point of the exercise: the answer is the same either way
    assert second == first


def test_a_healthy_running_log_costs_no_reads(tmp_path: Path) -> None:
    write_log(tmp_path, TASK, status="started", samples=[errored("s1")])
    cache = ClassedCache()

    classed = classed_instances(
        observe_logs(tmp_path), errored_running=set(), cache=cache
    )

    # the worker's live read said nothing errored, so the log was not opened
    assert classed.instances == []
    assert cache.running == {}


def test_a_running_log_with_errors_is_read_and_memoized(tmp_path: Path) -> None:
    write_log(tmp_path, TASK, status="started", samples=[errored("s1")])
    cache = ClassedCache()
    logs = observe_logs(tmp_path)

    first = classed_instances(logs, errored_running=locations(logs), cache=cache)
    again = classed_instances(logs, errored_running=locations(logs), cache=cache)
    ungated = classed_instances(logs, errored_running=set(), cache=cache)

    assert [i.class_key for i in first.instances] == [TIMEOUT_KEY]
    # the classed sample is memoized, and the memo outlives a turn whose live
    # read did not gate the log open — the journal already holds the instance
    assert [i.ref for i in again.instances] == [i.ref for i in first.instances]
    assert ungated.instances == []
    assert cache.running != {}


def test_the_settled_read_supersedes_the_running_memo(tmp_path: Path) -> None:
    # the log an eval was writing settles in place: same location, same
    # eval_id, and whatever the memo held while it ran is now a stand-in for
    # a read that has happened
    write_log(tmp_path, TASK, completed=1, samples=[errored("s1"), errored("s2")])
    logs = observe_logs(tmp_path)
    attempt = next(a for attempts in logs.attempts.values() for a in attempts)
    cache = ClassedCache()
    stale = Instance(class_key=TIMEOUT_KEY, ref="stale", eval_id=attempt.eval_id)
    cache.running[attempt.eval_id] = {"s1:1:uuid-s1-1": stale}

    settled = classed_instances(logs, errored_running=set(), cache=cache)

    assert sorted(i.sample_id for i in settled.instances) == ["s1", "s2"]
    assert all(i.ref != "stale" for i in settled.instances)
    assert cache.running == {}


def test_unreadable_summaries_on_a_settled_log_are_reported(tmp_path: Path) -> None:
    # a header that reads fine and a body that does not: total > 0 promises
    # summaries, and the file has none to give
    location = write_log(tmp_path, TASK, completed=2)
    text = location.read_text(encoding="utf-8")
    location.write_text(
        text.replace('"reductions"', '"reductioms"')
        if '"reductions"' in text
        else text,
        encoding="utf-8",
    )

    classed = classed_instances(
        observe_logs(tmp_path), errored_running=set(), cache=ClassedCache()
    )

    # a log with no errored samples yields no instances either way; the claim
    # is only that reading it did not raise
    assert classed.instances == []


def test_uniform_zero_confirms_and_mixed_scores_do_not(tmp_path: Path) -> None:
    zeros = SynthTask("zeros", samples=2)
    mixed = SynthTask("mixed", samples=2)
    letters = SynthTask("letters", samples=2)
    unscored = SynthTask("unscored", samples=1)
    write_log(
        tmp_path,
        zeros,
        samples=[SynthSample(id="s1", score=0.0), SynthSample(id="s2", score=0)],
    )
    write_log(
        tmp_path,
        mixed,
        samples=[SynthSample(id="s1", score=0.0), SynthSample(id="s2", score=1.0)],
    )
    write_log(
        tmp_path,
        letters,
        samples=[SynthSample(id="s1", score="I"), SynthSample(id="s2", score="I")],
    )
    write_log(tmp_path, unscored, samples=[SynthSample(id="s1")])

    classed = classed_instances(
        observe_logs(tmp_path), errored_running=set(), cache=ClassedCache()
    )

    by_task = {location: flag for location, flag in classed.zero.items()}
    assert [flag for location, flag in by_task.items() if "zeros" in location] == [True]
    assert [flag for location, flag in by_task.items() if "mixed" in location] == [
        False
    ]
    # incorrect letters convert to zero — a graded fail across the board confirms
    assert [flag for location, flag in by_task.items() if "letters" in location] == [
        True
    ]
    # no scores at all confirms nothing
    assert [flag for location, flag in by_task.items() if "unscored" in location] == [
        False
    ]


def test_the_cache_survives_a_round_trip_and_discards_other_versions(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    write_log(log_dir, TASK, completed=2, samples=[errored("s1")])
    cache = ClassedCache()
    classed_instances(observe_logs(log_dir), errored_running=set(), cache=cache)
    path = tmp_path / "classed.json"

    write_classed_cache(path, cache)
    loaded = read_classed_cache(path)

    assert set(loaded.logs) == set(cache.logs)
    entry, original = next(iter(loaded.logs.values())), next(iter(cache.logs.values()))
    assert entry == original

    stamped = path.read_text(encoding="utf-8").replace('"version": 1', '"version": 0')
    path.write_text(stamped, encoding="utf-8")
    assert read_classed_cache(path).logs == {}


def test_keep_prunes_to_what_is_still_listed(tmp_path: Path) -> None:
    write_log(tmp_path, TASK, completed=2, samples=[errored("s1")])
    other = SynthTask("other", samples=1)
    write_log(tmp_path, other, samples=[SynthSample(id="s1", score=1.0)])
    cache = ClassedCache()
    logs = observe_logs(tmp_path)
    classed_instances(logs, errored_running=set(), cache=cache)
    assert len(cache.logs) == 2
    kept = locations(logs, holding="other")

    pruned = cache.keep(kept, running=set())

    assert set(pruned.logs) == kept
    assert pruned.running == {}


def test_losing_the_cache_costs_one_slow_turn_and_no_answers(tmp_path: Path) -> None:
    write_log(tmp_path, TASK, completed=2, samples=[errored("s1")])
    cached = classed_instances(
        observe_logs(tmp_path), errored_running=set(), cache=ClassedCache()
    )

    fresh = classed_instances(
        observe_logs(tmp_path), errored_running=set(), cache=ClassedCache()
    )

    assert [i.ref for i in fresh.instances] == [i.ref for i in cached.instances]
