"""A log directory read as state, and the fixtures that produce one.

Every case here is synthesized: no eval runs, no worker is launched, and the
whole file is a few dozen sub-kilobyte files in `tmp_path`. That is the point
of the generator (testing.md, *the fixture generator is the highest-leverage
thing to build*) — the eight states a log directory can be in are eight
function calls rather than eight process launches.

The reader and the generator are tested together because neither is testable
alone: a generator's only proof is that a reader agrees with it.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from inspect_ai.util._sandbox.environment import SandboxEnvironmentSpec
from inspect_steward._evalset.observe import (
    IncompleteReason,
    ObservedLogs,
    TaskState,
    observe_logs,
    observe_tasks,
)

from .._logs import (
    SynthTask,
    synth_manifest,
    write_log,
    write_running_eval,
    write_unreadable,
)

TASK = SynthTask("probe", samples=10, epochs=1)

EARLIER = "2026-08-23T18:00:00+00:00"
LATER = "2026-08-23T20:00:00+00:00"
LATEST = "2026-08-23T22:00:00+00:00"


def observe(
    log_dir: Path, *tasks: SynthTask
) -> list[tuple[TaskState, IncompleteReason | None]]:
    """Read a directory against a manifest naming `tasks`, in manifest order then orphans."""
    observed = observe_tasks(synth_manifest(tasks), observe_logs(log_dir))
    return [(task.state, task.reason) for task in observed.tasks]


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        pytest.param({}, (TaskState.COMPLETE, None), id="complete_clean"),
        pytest.param(
            {"completed": 7}, (TaskState.COMPLETE, None), id="complete_with_errors"
        ),
        pytest.param(
            {"total": 6},
            (TaskState.INCOMPLETE, IncompleteReason.SHORT),
            id="complete_but_short",
        ),
        pytest.param(
            {"status": "started"},
            (TaskState.INCOMPLETE, IncompleteReason.STARTED),
            id="started_never_finished",
        ),
        pytest.param(
            {"invalidated": True},
            (TaskState.INCOMPLETE, IncompleteReason.INVALIDATED),
            id="invalidated",
        ),
        pytest.param(
            {"error": "the provider went away"},
            (TaskState.INCOMPLETE, IncompleteReason.ERROR),
            id="error",
        ),
        pytest.param(
            {"status": "cancelled", "total": 4},
            (TaskState.INCOMPLETE, IncompleteReason.CANCELLED),
            id="cancelled",
        ),
        pytest.param(
            {"total": 0},
            (TaskState.INCOMPLETE, IncompleteReason.NO_RESULTS),
            id="no_results",
        ),
        pytest.param(None, (TaskState.MISSING, None), id="missing"),
    ],
)
def test_states(
    log: dict[str, Any] | None,
    expected: tuple[TaskState, IncompleteReason | None],
    tmp_path: Path,
) -> None:
    if log is not None:
        write_log(tmp_path, TASK, **log)

    assert observe(tmp_path, TASK) == [expected]


def test_errored_samples_are_a_count_not_a_state(tmp_path: Path) -> None:
    # worker mode forces fail_on_error=False, so a task finishes success
    # carrying its errored samples: they are step 23's anomalies, not a reason to
    # run the task again
    write_log(tmp_path, TASK, completed=7)

    observed = observe_tasks(synth_manifest([TASK]), observe_logs(tmp_path))

    assert observed.tasks[0].state == TaskState.COMPLETE
    assert observed.tasks[0].errored_samples == 3
    assert observed.tasks[0].required_samples == 10


def test_attempts_order_by_created(tmp_path: Path) -> None:
    for created in (LATER, EARLIER, LATEST):
        write_log(tmp_path, TASK, status="error", error="boom", created=created)

    logs = observe_logs(tmp_path)

    assert [attempt.created for attempt in logs.attempts[TASK.identifier]] == [
        LATEST,
        LATER,
        EARLIER,
    ]
    current = logs.current(TASK.identifier)
    assert current is not None and current.created == LATEST
    assert len(logs.superseded(TASK.identifier)) == 2


def test_the_modification_time_is_in_milliseconds(tmp_path: Path) -> None:
    # `EvalLogInfo` normalizes every backend's answer to milliseconds, and the
    # only reader of this field turns it back into an instant -- so the unit is
    # load-bearing and reads as seconds without complaint, landing in the year
    # 58614 rather than raising
    written = write_log(tmp_path, TASK)
    stamped = datetime(2026, 8, 23, 21, 0, tzinfo=timezone.utc).timestamp()
    os.utime(written, (stamped, stamped))

    (attempt,) = observe_logs(tmp_path).attempts[TASK.identifier]

    assert attempt.mtime == pytest.approx(stamped * 1000)


def test_the_latest_successful_attempt_wins(tmp_path: Path) -> None:
    # deliberately not upstream's rule, which takes the newest attempt whatever
    # its status: a re-run that errored must not displace a good result
    write_log(tmp_path, TASK, created=EARLIER)
    write_log(tmp_path, TASK, status="error", error="boom", created=LATER)

    observed = observe_tasks(synth_manifest([TASK]), observe_logs(tmp_path))
    task = observed.tasks[0]

    assert task.state == TaskState.COMPLETE
    assert task.current is not None and task.current.created == EARLIER
    assert [attempt.created for attempt in task.superseded] == [LATER]


def test_an_orphan_keeps_its_attempts(tmp_path: Path) -> None:
    # an identifier the definition no longer names: the archive path, which
    # needs the paths as well as the fact
    removed = SynthTask("removed")
    write_log(tmp_path, TASK)
    write_log(tmp_path, removed, created=EARLIER)
    write_log(tmp_path, removed, status="error", error="boom", created=LATER)

    observed = observe_tasks(synth_manifest([TASK]), observe_logs(tmp_path))
    orphan = observed.tasks[-1]

    assert [task.state for task in observed.tasks] == [
        TaskState.COMPLETE,
        TaskState.ORPHANED,
    ]
    assert orphan.identifier == removed.identifier
    assert orphan.task is None
    assert orphan.key == "removed"
    assert orphan.current is not None and len(orphan.superseded) == 1


def test_an_unreadable_log_costs_one_log(tmp_path: Path) -> None:
    # the journal's rule, applied to the other thing Steward reads on a
    # schedule: a tend that raised on a half-written file is a tend that never
    # ran
    write_log(tmp_path, TASK)
    broken = write_unreadable(tmp_path)

    logs = observe_logs(tmp_path)

    assert logs.count == 1
    assert not logs.intact
    assert len(logs.unreadable) == 1
    assert logs.unreadable[0].location.endswith(broken.name)
    assert logs.unreadable[0].reason
    # and it reaches the caller that decides whether to complain
    assert observe_tasks(synth_manifest([TASK]), logs).unreadable == logs.unreadable


def test_a_mid_run_eval_has_no_header(tmp_path: Path) -> None:
    # an .eval being written has no header.json; the reader falls back to
    # _journal/start.json, which still carries everything the identifier needs
    write_running_eval(tmp_path, TASK, created=LATER)

    logs = observe_logs(tmp_path)
    attempt = logs.current(TASK.identifier)

    assert attempt is not None
    assert attempt.identifier == TASK.identifier
    assert attempt.status == "started"
    assert attempt.created == LATER
    assert attempt.total_samples == 0
    assert observe(tmp_path, TASK) == [(TaskState.INCOMPLETE, IncompleteReason.STARTED)]


def test_json_and_eval_agree_on_the_identifier(tmp_path: Path) -> None:
    # the guard on the generator itself: cheap json fixtures are only worth
    # anything if they identify the same way the format production writes does
    write_log(tmp_path, TASK, created=EARLIER, format="json")
    write_log(tmp_path, TASK, created=LATER, format="eval")

    logs = observe_logs(tmp_path)

    assert list(logs.attempts) == [TASK.identifier]
    assert logs.count == 2


def test_a_missing_directory_is_an_empty_observation(tmp_path: Path) -> None:
    logs = observe_logs(tmp_path / "never-created")

    assert logs.attempts == {}
    assert logs.intact
    assert observe(tmp_path / "never-created", TASK) == [(TaskState.MISSING, None)]


@pytest.mark.parametrize(
    ("manifest_epochs", "log_epochs", "expected"),
    [
        (1, 1, (TaskState.COMPLETE, None)),
        (3, 1, (TaskState.INCOMPLETE, IncompleteReason.SHORT)),
        (3, 3, (TaskState.COMPLETE, None)),
    ],
)
def test_raising_epochs_makes_the_same_task_incomplete(
    manifest_epochs: int,
    log_epochs: int,
    expected: tuple[TaskState, IncompleteReason | None],
    tmp_path: Path,
) -> None:
    # epochs are held outside task_identifier deliberately, so this is the same
    # task with more work to do rather than a new one (workflow.md §2.1)
    task = SynthTask("probe", samples=10, epochs=manifest_epochs)
    write_log(tmp_path, task, total=10 * log_epochs, epochs=log_epochs)

    assert observe(tmp_path, task) == [expected]
    assert len(observe_logs(tmp_path).attempts) == 1


def test_a_directory_with_no_manifest_reads_fine(tmp_path: Path) -> None:
    # logs-archive/ and the flow store key on identifier alone, which is why
    # the manifest is not an argument to the read
    archive = tmp_path / "logs-archive"
    write_log(archive, TASK)
    write_log(archive, SynthTask("removed"))

    logs = observe_logs(archive)

    assert isinstance(logs, ObservedLogs)
    assert len(logs.attempts) == 2
    assert logs.intact


def test_scan_output_underneath_the_log_dir_is_not_a_log(tmp_path: Path) -> None:
    # a log directory is flat by design and scans live beneath it, so the read
    # is deliberately not recursive
    write_log(tmp_path, TASK)
    write_log(tmp_path / "scans" / "scan_id=abc", SynthTask("scanned"))

    assert list(observe_logs(tmp_path).attempts) == [TASK.identifier]


RESHAPING: list[tuple[str, dict[str, Any], dict[str, Any], IncompleteReason | None]] = [
    ("nobody selected anything", {}, {}, None),
    ("the same slice", {"limit": 3}, {"limit": 3}, None),
    ("the same range, listed either way", {"limit": (0, 5)}, {"limit": [0, 5]}, None),
    ("a slice where there was none", {"limit": 3}, {}, IncompleteReason.RESHAPED),
    ("no slice where there was one", {}, {"limit": 3}, IncompleteReason.RESHAPED),
    (
        "a different range of the same size",
        {"limit": (5, 10)},
        {"limit": [0, 5]},
        IncompleteReason.RESHAPED,
    ),
    (
        "a reshuffle",
        {"limit": 3, "sample_shuffle": 42},
        {"limit": 3, "sample_shuffle": 7},
        IncompleteReason.RESHAPED,
    ),
    (
        "different ids",
        {"sample_id": ["a", "b"]},
        {"sample_id": ["a", "c"]},
        IncompleteReason.RESHAPED,
    ),
]


@pytest.mark.parametrize(
    ("wanted", "ran", "expected"),
    [(wanted, ran, expected) for _, wanted, ran, expected in RESHAPING],
    ids=[case for case, _, _, _ in RESHAPING],
)
def test_a_log_answering_a_different_question_is_incomplete(
    wanted: dict[str, Any],
    ran: dict[str, Any],
    expected: IncompleteReason | None,
    tmp_path: Path,
) -> None:
    """The gap a sample count cannot see, and neither can an identifier.

    `limit`, `sample_id` and `sample_shuffle` are all outside
    `task_identifier` on purpose — that is what lets a raised limit resume
    rather than orphan what has run. The cost is that changing *which* samples
    run keeps the identifier and can keep the count, so a re-launch that
    reshuffles a limited run would otherwise report nothing to do and sign the
    previous subset off as the answer.
    """
    task = SynthTask("probe", samples=3)
    write_log(tmp_path, task, total=3, selection=ran)
    manifest = synth_manifest([task], **wanted)

    observed = observe_tasks(manifest, observe_logs(tmp_path))

    (only,) = observed.tasks
    assert only.reason == expected
    assert only.state == (
        TaskState.COMPLETE if expected is None else TaskState.INCOMPLETE
    )


def test_a_manifest_that_never_recorded_a_field_is_not_read_as_a_change(
    tmp_path: Path,
) -> None:
    """An older capture said nothing about `sample_shuffle`, which is not the same as saying no.

    Reading the absence as `None` marks a shuffled run stale on the day the
    reader is upgraded — and permanently, because the manifest the re-run
    commits records the same nothing. So the definition side is consulted only
    where the key is there.
    """
    task = SynthTask("probe", samples=3)
    write_log(tmp_path, task, total=3, selection={"sample_shuffle": 42})
    manifest = synth_manifest([task])
    older = manifest.model_copy(
        update={
            "options": {
                key: value
                for key, value in manifest.options.items()
                if key != "sample_shuffle"
            }
        }
    )

    (only,) = observe_tasks(older, observe_logs(tmp_path)).tasks

    assert only.reason is None
    # and the key being present with no value still compares, which is what
    # tells a definition that shuffles from a manifest that cannot say
    (current,) = observe_tasks(manifest, observe_logs(tmp_path)).tasks
    assert current.reason is IncompleteReason.RESHAPED


def test_a_task_qualified_sample_id_is_resolved_before_it_is_compared(
    tmp_path: Path,
) -> None:
    """`eval_run` strips the qualifier per task before the log records what ran.

    So `--sample-id probe:a,other:b` reaches the manifest whole and the log as
    `["a"]`. Compared unresolved the two never match, and a task that ran
    exactly what was asked of it is re-run every tend until the attempt budget
    is spent.
    """
    # one sample, because capture counts the selection too -- the manifest a
    # real run commits asks for exactly the sample the log holds
    task = SynthTask("probe", samples=1)
    write_log(tmp_path, task, total=1, selection={"sample_id": ["a"]})
    manifest = synth_manifest([task], sample_id=["probe:a", "other:b"])

    (only,) = observe_tasks(manifest, observe_logs(tmp_path)).tasks

    assert only.reason is None


REDIRECTION: list[tuple[str, dict[str, Any], dict[str, Any], bool]] = [
    (
        "a gateway that has not moved",
        {"model_base_url": "https://gw.example/v1"},
        {"model_base_url": "https://gw.example/v1"},
        False,
    ),
    (
        "a gateway that has",
        {"model_base_url": "https://other.example/v1"},
        {"model_base_url": "https://gw.example/v1"},
        True,
    ),
    (
        "a gateway where the log had none",
        {"model_base_url": "https://gw.example/v1"},
        {},
        True,
    ),
    (
        "a type-only override against the config it resolved to",
        {"sandbox": SandboxEnvironmentSpec("docker")},
        {"sandbox": SandboxEnvironmentSpec("docker", "compose.yaml")},
        False,
    ),
    (
        "a different sandbox type",
        {"sandbox": SandboxEnvironmentSpec("k8s")},
        {"sandbox": SandboxEnvironmentSpec("docker", "compose.yaml")},
        True,
    ),
    (
        "a named config against the same one",
        {"sandbox": SandboxEnvironmentSpec("docker", "compose.yaml")},
        {"sandbox": SandboxEnvironmentSpec("docker", "compose.yaml")},
        False,
    ),
    (
        "a named config against a different one",
        {"sandbox": SandboxEnvironmentSpec("docker", "other.yaml")},
        {"sandbox": SandboxEnvironmentSpec("docker", "compose.yaml")},
        True,
    ),
    (
        "the same sandbox said the short way",
        {"sandbox": "docker"},
        {"sandbox": SandboxEnvironmentSpec("docker")},
        False,
    ),
]


@pytest.mark.parametrize(
    ("overridden", "ran", "reshaped"),
    [(overridden, ran, reshaped) for _, overridden, ran, reshaped in REDIRECTION],
    ids=[case for case, _, _, _ in REDIRECTION],
)
def test_an_override_pointing_a_task_elsewhere_makes_its_log_incomplete(
    overridden: dict[str, Any],
    ran: dict[str, Any],
    reshaped: bool,
    tmp_path: Path,
) -> None:
    """A different image or a different gateway is a different answer.

    Both are identity-neutral upstream, so the identifier still pairs the log
    with the task and the count still agrees. The cases that must *not* fire
    are the load-bearing ones: a log records the sandbox resolved, so a
    type-only override compared whole would mark every run with a
    `compose.yaml` beside it stale forever.
    """
    task = SynthTask("probe", samples=3)
    write_log(tmp_path, task, total=3, **ran)
    manifest = synth_manifest([task]).model_copy(
        update={"overrides": EvalSetOverrides(**overridden)}
    )

    (only,) = observe_tasks(manifest, observe_logs(tmp_path)).tasks

    assert (only.reason is IncompleteReason.REDIRECTED) is reshaped


def test_the_definition_s_own_sandbox_is_never_compared(tmp_path: Path) -> None:
    # nothing records what the definition asked for, so there is nothing to
    # compare -- and comparing the log's own value against the absent override
    # would call every sandboxed run stale from its first tend
    task = SynthTask("probe", samples=3)
    write_log(
        tmp_path, task, total=3, sandbox=SandboxEnvironmentSpec("docker", "c.yaml")
    )

    (only,) = observe_tasks(synth_manifest([task]), observe_logs(tmp_path)).tasks

    assert only.reason is None


def test_an_override_outranks_the_definition_in_the_comparison(tmp_path: Path) -> None:
    # `options` is what the definition passed and `overrides` is what this run
    # replaced it with, so the log has to be measured against the second where
    # there is one -- otherwise every overridden run reads as reshaped forever
    task = SynthTask("probe", samples=3)
    write_log(tmp_path, task, total=3, selection={"limit": 3})
    manifest = synth_manifest([task], limit=(0, 9)).model_copy(
        update={"overrides": EvalSetOverrides(limit=3)}
    )

    (only,) = observe_tasks(manifest, observe_logs(tmp_path)).tasks

    assert only.reason is None


def test_a_redirect_outranks_a_reason_that_would_resume(tmp_path: Path) -> None:
    """A short *and* redirected log has to reset, not resume.

    Every other reason leaves the log's finished samples worth keeping, so the
    spawn hands the worker its prior log. Letting `SHORT` answer first would do
    exactly that for a log whose every answer is stale — and since the sample
    set is unchanged, the worker would find all of them, reuse all of them, and
    finish having run nothing.
    """
    task = SynthTask("probe", samples=5)
    write_log(tmp_path, task, total=2, model_base_url="https://old.example/v1")
    manifest = synth_manifest([task]).model_copy(
        update={"overrides": EvalSetOverrides(model_base_url="https://new.example/v1")}
    )

    (only,) = observe_tasks(manifest, observe_logs(tmp_path)).tasks

    assert only.reason is IncompleteReason.REDIRECTED


def test_a_shape_returned_to_is_answered_by_the_attempt_that_answered_it(
    tmp_path: Path,
) -> None:
    """Slice A, then B, then A again — and A's result never left the directory.

    The newest successful attempt answers B, so taking it re-runs three samples
    that are sitting one file away. The rule is right where it was written, for
    a directory with no manifest to compare against; here there is one, and it
    says which attempt is the answer.
    """
    task = SynthTask("probe", samples=3)
    write_log(tmp_path, task, total=3, selection={"limit": (0, 3)}, created=EARLIER)
    write_log(tmp_path, task, total=3, selection={"limit": (3, 6)}, created=LATER)

    (only,) = observe_tasks(
        synth_manifest([task], limit=(0, 3)), observe_logs(tmp_path)
    ).tasks

    assert only.state is TaskState.COMPLETE
    assert only.reason is None
    assert only.current is not None and only.current.created == EARLIER
    assert [attempt.created for attempt in only.superseded] == [LATER]


def test_a_gateway_returned_to_is_answered_the_same_way(tmp_path: Path) -> None:
    # and it matters more here than for a slice: a redirected task is spawned
    # without its prior log, so re-running it is the whole task from nothing
    task = SynthTask("probe", samples=3)
    write_log(
        tmp_path, task, total=3, model_base_url="https://a.example/v1", created=EARLIER
    )
    write_log(
        tmp_path, task, total=3, model_base_url="https://b.example/v1", created=LATER
    )
    manifest = synth_manifest([task]).model_copy(
        update={"overrides": EvalSetOverrides(model_base_url="https://a.example/v1")}
    )

    (only,) = observe_tasks(manifest, observe_logs(tmp_path)).tasks

    assert only.reason is None
    assert only.current is not None and only.current.created == EARLIER


def test_where_no_attempt_answers_the_newest_successful_one_still_does(
    tmp_path: Path,
) -> None:
    # the fallback, and it is the rule that was always here: some log has to be
    # the one resumed, and *newest successful* chooses better than nothing
    task = SynthTask("probe", samples=3)
    write_log(tmp_path, task, total=3, selection={"limit": (0, 3)}, created=EARLIER)
    write_log(tmp_path, task, total=3, selection={"limit": (3, 6)}, created=LATER)

    (only,) = observe_tasks(
        synth_manifest([task], limit=(6, 9)), observe_logs(tmp_path)
    ).tasks

    assert only.reason is IncompleteReason.RESHAPED
    assert only.current is not None and only.current.created == LATER


def test_a_changed_slice_is_still_reshaped_rather_than_redirected(
    tmp_path: Path,
) -> None:
    # the distinction is the action: a moved slice resumes, because the samples
    # still wanted were answered under settings still in force
    task = SynthTask("probe", samples=3)
    write_log(tmp_path, task, total=3, selection={"limit": 3})
    manifest = synth_manifest([task], limit=(5, 8))

    (only,) = observe_tasks(manifest, observe_logs(tmp_path)).tasks

    assert only.reason is IncompleteReason.RESHAPED
