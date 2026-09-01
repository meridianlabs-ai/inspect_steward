"""Carrying out an acceptance: the log says what the person decided about it.

A `rerun` ruling acts on samples; an `accept` acts on the **log** — it says *this attempt is the result, with a caveat the report carries* — and until step 26 the log on disk went on saying `error` while Steward's own state said accepted. These are the claims worth defending: the amendment marks the header and **leaves every sample where it was** (the one mistake here would destroy a result rather than fail to change one), the ruling travels inside the log as provenance, the halting error moves rather than being kept or dropped, and the whole thing is idempotent from either of its two witnesses — the journal record, and the log's own status.

Real logs throughout, in the `.eval` zip format, because the in-place header swap is the thing being tested and a JSON document would not exercise it.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.log import read_eval_log
from inspect_steward._anomaly.applied import RULING_APPLIED, read_applied
from inspect_steward._anomaly.model import (
    Anomalies,
    Anomaly,
    AnomalyState,
    Disposition,
    Evidence,
    Ruling,
)
from inspect_steward._evalset.observe import (
    ObservedTasks,
    TaskState,
    observe_logs,
    observe_tasks,
)
from inspect_steward._schedule import InFlight
from inspect_steward._tend.items import SIGNOFF_READY, Verdict
from inspect_steward._tend.rulings import ACCEPTANCE_KEY, accepted_tasks, apply_rulings
from inspect_steward._worker import LiveFleet
from inspect_steward._workspace import (
    ACTION,
    LAUNCHED,
    PAUSED,
    RULING,
    Workspace,
    append_event,
    read_journal,
)

from .._logs import SynthSample, SynthTask, synth_manifest, write_log
from ..schedule.test_reconcile import live
from ..schedule.test_tend import prepared, turn
from .test_items import CLASS as SAMPLE_CLASS
from .test_items import errored
from .test_rulings import FakeActed

SANDBOX_TRACEBACK = """Traceback (most recent call last):
  File "/work/inspect_ai/util/docker/_sandbox.py", line 88, in start
    raise RuntimeError("the sandbox would not start")
RuntimeError: the sandbox would not start
"""

CLASS = "task:error:RuntimeError@docker/_sandbox.py:start"
"""What `task_error_class` makes of `SANDBOX_TRACEBACK`, and it has to be exactly that: the executor amends a log only where the failure on disk is the one somebody accepted."""

ACCEPTED_AT = "2026-08-31T02:00:00Z"
REASON = "the sandbox host is gone for the night; 8 of 10 samples is enough"
EFFECT = "this arm's remaining samples are not in the data"

TASK = SynthTask("probe", samples=3)


def window(
    *,
    class_key: str = CLASS,
    kind: str = "task",
    ts: str = ACCEPTED_AT,
    tasks: tuple[str, ...] = (),
    disposition: Disposition = Disposition.ACCEPT,
    generation: int = 1,
) -> Anomaly:
    """A settled window carrying an accepting ruling, as the fold would build it."""
    return Anomaly(
        class_key=class_key,
        kind=kind,
        state=AnomalyState.ACCEPTED,
        evidence=Evidence(count=1, tasks=tasks),
        generation=generation,
        ruling=Ruling(
            class_key=class_key,
            disposition=disposition,
            reason=REASON,
            by="kaia",
            ts=ts,
            effect=EFFECT,
        ),
    )


def accepting(
    root: Path,
    *,
    status: str = "error",
    error: str | None = "the sandbox would not start",
    samples: int = 3,
) -> tuple[Workspace, ObservedTasks, str]:
    """A workspace whose one task landed a real `.eval` log in the given state."""
    workspace, manifest = prepared(root, [TASK])
    location = write_log(
        workspace.logs,
        TASK,
        format="eval",
        status=status,  # type: ignore[arg-type]
        error=error,
        error_traceback=SANDBOX_TRACEBACK if error else None,
        completed=samples,
        samples=[SynthSample(id=f"s{n}") for n in range(samples)],
    )
    observed = observe_tasks(manifest, observe_logs(workspace.logs))
    return workspace, observed, str(location)


def accept(
    workspace: Workspace,
    observed: ObservedTasks,
    anomalies: Anomalies,
    *,
    running: tuple[str, ...] = (),
    spawned: set[str] | None = None,
) -> FakeActed:
    acted = FakeActed()
    apply_rulings(
        workspace,
        anomalies,
        [],
        read_applied(read_journal(workspace.journal).events),
        InFlight(running=[live(identifier) for identifier in running]),
        LiveFleet(tasks={}),
        observed,
        spawned or set(),
        acted,
    )
    return acted


def acceptances(workspace: Workspace) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == ACTION
        and event.payload.get("action") == RULING_APPLIED
        and "accepted" in event.payload
    ]


def identifier(observed: ObservedTasks) -> str:
    return observed.tasks[0].identifier


def test_accepting_a_failed_task_marks_its_log_success_and_leaves_every_sample(
    tmp_path: Path,
) -> None:
    """The header says what was decided; the samples are untouched.

    The second half is the one that matters. A header-only read followed by a
    write that forgets `header_only=True` re-initialises the recorder and
    persists `samples or []` — which, over that read, is nothing at all. The
    two calls are individually reasonable and together destroy the log, so the
    sample count is asserted here rather than assumed anywhere.
    """
    workspace, observed, location = accepting(tmp_path)
    ruled = Anomalies(settled=(window(tasks=(identifier(observed),)),))

    accept(workspace, observed, ruled)

    log = read_eval_log(location)
    assert log.status == "success"
    assert len(log.samples or []) == 3


def test_the_ruling_travels_inside_the_log_it_covers(tmp_path: Path) -> None:
    # a reader who has only the log — six months on, in another project —
    # still learns that this was accepted, by whom, and why
    workspace, observed, location = accepting(tmp_path)
    ruled = Anomalies(settled=(window(tasks=(identifier(observed),)),))

    accept(workspace, observed, ruled)

    log = read_eval_log(location, header_only=True)
    record = log.metadata[ACCEPTANCE_KEY]
    assert record["reason"] == REASON
    assert record["class"] == CLASS
    assert record["effect"] == EFFECT
    assert log.log_updates is not None
    assert log.log_updates[-1].provenance.author == "kaia"


def test_the_halting_error_moves_into_the_acceptance_rather_than_vanishing(
    tmp_path: Path,
) -> None:
    """An error payload on a success row is a contradiction; losing it is worse.

    So it goes into the record verbatim and comes off the header, which is the
    field every listing and the viewer read.
    """
    workspace, observed, location = accepting(tmp_path)
    ruled = Anomalies(settled=(window(tasks=(identifier(observed),)),))

    accept(workspace, observed, ruled)

    log = read_eval_log(location, header_only=True)
    assert log.error is None
    assert (
        log.metadata[ACCEPTANCE_KEY]["error_before"]["message"]
        == "the sandbox would not start"
    )
    assert log.metadata[ACCEPTANCE_KEY]["status_before"] == "error"


def test_a_second_turn_does_not_amend_the_log_twice(tmp_path: Path) -> None:
    workspace, observed, location = accepting(tmp_path)
    ruled = Anomalies(settled=(window(tasks=(identifier(observed),)),))

    accept(workspace, observed, ruled)
    stamped = Path(location).stat().st_mtime_ns
    accept(workspace, observed, ruled)

    assert len(acceptances(workspace)) == 1
    assert Path(location).stat().st_mtime_ns == stamped


def test_a_lost_record_books_off_the_log_rather_than_amending_it_again(
    tmp_path: Path,
) -> None:
    """The crash between the effect and the journal append.

    The amendment landed and the record did not, so the memory is gone — and
    the log's own status is the second witness that stops a header being
    swapped twice.
    """
    workspace, observed, location = accepting(tmp_path)
    ruled = Anomalies(settled=(window(tasks=(identifier(observed),)),))
    accept(workspace, observed, ruled)
    workspace.journal.write_text("", encoding="utf-8")
    stamped = Path(location).stat().st_mtime_ns

    observed = observe_tasks(synth_manifest([TASK]), observe_logs(workspace.logs))
    accept(workspace, observed, ruled)

    (event,) = acceptances(workspace)
    (booked,) = event["accepted"]
    assert booked["task"] == identifier(observed)
    assert (booked["flipped"], booked["note"]) == (False, "already success")
    assert Path(location).stat().st_mtime_ns == stamped


@pytest.mark.parametrize(
    ("status", "error"),
    [
        pytest.param("success", None, id="success"),
        pytest.param("error", "the sandbox would not start", id="errored"),
    ],
)
def test_a_non_task_acceptance_never_amends_the_log_and_still_witnesses(
    tmp_path: Path, status: str, error: str | None
) -> None:
    """A `limit:` or `score:` acceptance is a claim about the data *inside* a log.

    The `success` row is where most of them land — there is no mark to make,
    and without the record the class would be re-examined every turn for the
    life of the run. The `error` row is the one that made this a gate rather
    than a guard: a log holding operator-killed samples can also terminate with
    a task failure, and the old rule — every accepted kind, guarded by the
    log's own status — cleared that failure under a decision about something
    else, moving its account into the acceptance metadata and leaving a header
    that read `success` for a task nobody had accepted.
    """
    workspace, observed, location = accepting(
        tmp_path, status=status, error=error, samples=3
    )
    ruled = Anomalies(
        settled=(
            window(
                class_key="limit:operator", kind="limit", tasks=(identifier(observed),)
            ),
        )
    )
    stamped = Path(location).stat().st_mtime_ns

    accept(workspace, observed, ruled)

    (event,) = acceptances(workspace)
    assert (
        event["accepted"][0]["note"] == "the log's own failure is not what was accepted"
    )
    assert Path(location).stat().st_mtime_ns == stamped
    log = read_eval_log(location, header_only=True)
    assert log.status == status
    assert (log.error.message if log.error else None) == error
    # and it is booked all the same, or the class is re-examined every turn
    fold = read_applied(read_journal(workspace.journal).events)
    assert fold.accepted_tasks("limit:operator", ACCEPTED_AT) == {identifier(observed)}


def test_a_log_that_now_fails_differently_is_not_amended(tmp_path: Path) -> None:
    """The same mistake one level down, and the same answer.

    Somebody accepted *this task fails like this*. If the attempt on disk is
    failing some other way — a re-run landed a different error — flipping it to
    `success` would clear a failure nobody has seen, let alone ruled on. The
    acceptance waits for a decision about the failure that is actually there.
    """
    workspace, observed, location = accepting(
        tmp_path, error="the sandbox would not start"
    )
    elsewhere = replace(
        window(tasks=(identifier(observed),)),
        class_key="task:error:ScorerError@evals/scorer.py:score",
    )

    accept(workspace, observed, Anomalies(settled=(elsewhere,)))

    (event,) = acceptances(workspace)
    assert event["accepted"][0]["note"].startswith(f"the log now fails as {CLASS}")
    assert read_eval_log(location, header_only=True).status == "error"


def test_a_log_still_being_written_is_accepted_without_being_rewritten(
    tmp_path: Path,
) -> None:
    """`task:vanished` — the case the amendment must not touch.

    Upstream's in-place swap would *create* a header in a zip whose header is
    still the journal's start record: a manufactured finished log, with no
    results, claiming success. The record says the acceptance landed and the
    file stays exactly as honest as it was.
    """
    workspace, observed, location = accepting(tmp_path, status="started", error=None)
    ruled = Anomalies(settled=(window(tasks=(identifier(observed),)),))

    accept(workspace, observed, ruled)

    (event,) = acceptances(workspace)
    assert event["accepted"][0]["note"] == "the log is still being written"
    assert read_eval_log(location, header_only=True).status == "started"


@pytest.mark.parametrize(
    ("where", "why"),
    [
        pytest.param("running", "a worker is running it", id="running"),
        pytest.param("spawned", "a worker was just spawned for it", id="spawned"),
    ],
)
def test_a_task_a_worker_holds_defers_with_no_record(
    tmp_path: Path, where: str, why: str
) -> None:
    # a header swap rewrites a zip's central directory in place, and doing that
    # under a live worker is the one way this could destroy a result
    workspace, observed, location = accepting(tmp_path)
    name = identifier(observed)
    ruled = Anomalies(settled=(window(tasks=(name,)),))

    accept(
        workspace,
        observed,
        ruled,
        running=(name,) if where == "running" else (),
        spawned={name} if where == "spawned" else None,
    )

    assert acceptances(workspace) == []
    assert read_eval_log(location, header_only=True).status == "error"
    assert why in workspace.log.read_text(encoding="utf-8")


def test_a_failing_acceptance_costs_its_class_and_not_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, observed, _ = accepting(tmp_path)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("the log is read-only")

    monkeypatch.setattr("inspect_steward._tend.rulings.write_eval_log", refuse)
    acted = accept(
        workspace, observed, Anomalies(settled=(window(tasks=(identifier(observed),)),))
    )

    assert len(acted.failures) == 1
    assert "could not apply the acceptance" in acted.failures[0]
    assert acceptances(workspace) == []


def test_only_a_task_acceptance_stops_the_respawning(tmp_path: Path) -> None:
    """The latch is `task:` kinds only, and the executor is every kind.

    Accepting the operator kills inside a task that is *also* short for an
    unrelated reason must not silently end that task — that is a decision
    nobody made.
    """
    ident = "probe@openai/gpt-4o"
    settled = Anomalies(
        settled=(
            window(tasks=(ident,)),
            window(class_key="limit:operator", kind="limit", tasks=(ident,)),
            window(
                class_key="task:no-log",
                tasks=("other@openai/gpt-4o",),
                disposition=Disposition.DISMISS,
            ),
        )
    )

    assert set(accepted_tasks(settled)) == {ident}


# --- the latch, through real turns ----------------------------------------

SCORER_TRACEBACK = """Traceback (most recent call last):
  File "/work/evals/scorer.py", line 15, in score
    raise ScorerError("no grade")
evals.scorer.ScorerError: no grade
"""

FAILED_CLASS = "task:error:evals.scorer.ScorerError@evals/scorer.py:score"


def short_run(root: Path) -> Workspace:
    """A run whose one task died mid-way and left a short, errored log.

    Paused, so the respawn this would otherwise provoke stays a queue entry
    rather than a live worker — the latch is visible either way, and the test
    stays offline.
    """
    task = SynthTask("probe", samples=4)
    workspace, _ = prepared(root, [task])
    write_log(
        workspace.logs,
        task,
        format="eval",
        total=2,
        completed=2,
        error="ScorerError('no grade')",
        error_traceback=SCORER_TRACEBACK,
    )
    append_event(workspace.journal, PAUSED, by="test", reason="hold the respawn")
    return workspace


def accept_the_failure(workspace: Workspace) -> None:
    payload: dict[str, Any] = {
        "class": FAILED_CLASS,
        "disposition": "accept",
        "reason": REASON,
        "by": "kaia",
        "effect": EFFECT,
    }
    append_event(workspace.journal, RULING, **payload)


def test_accepting_a_dead_task_stops_the_respawning_without_signing_anything(
    tmp_path: Path,
) -> None:
    """The whole point of the latch: signoff is not what ends the retrying.

    Before the ruling the task is work Steward keeps queueing. After it,
    nothing is queued for it and the decision is reported in its place — and
    none of that waited for an attestation.
    """
    workspace = short_run(tmp_path)
    before = turn(workspace)
    assert before.summary.queued == 1
    assert before.summary.accepted == []

    accept_the_failure(workspace)
    after = turn(workspace)

    assert after.summary.queued == 0
    assert len(after.summary.accepted) == 1
    assert after.summary.stalled == []


def test_a_short_but_accepted_run_becomes_ready_to_sign_off(tmp_path: Path) -> None:
    """A task settled by decision counts toward the gate, or it never opens.

    The log is short and stays short — observation says so forever — so a gate
    keyed on completeness alone would make *accepting known holes* the one
    workflow this invitation could never invite.
    """
    workspace = short_run(tmp_path)
    assert SIGNOFF_READY not in {item.kind for item in turn(workspace).items}

    accept_the_failure(workspace)
    result = turn(workspace)

    item = next(item for item in result.items if item.kind == SIGNOFF_READY)
    assert "accepted as it stands" in item.summary
    # and the surface stays honest: the log really is short
    assert result.summary.states[TaskState.INCOMPLETE.value] == 1


def test_an_accepted_incomplete_run_is_not_reported_as_stopped(tmp_path: Path) -> None:
    # `verdict` calls a run with work left and nothing moving STOPPED, and an
    # accepted task is not work left -- without that, a signable run would
    # read 🛑 forever
    workspace = short_run(tmp_path)
    accept_the_failure(workspace)

    result = turn(workspace)

    assert result.verdict is not Verdict.STOPPED


def test_a_launch_puts_an_accepted_task_back_in_play(tmp_path: Path) -> None:
    """Committing a manifest is the one moment desired state is decided.

    A person who relaunches a run that re-asks for this task has said it should
    run again, and no record needs to be undone to say so — the accepting
    ruling simply stops postdating the launch.
    """
    workspace = short_run(tmp_path)
    turn(workspace)
    accept_the_failure(workspace)
    assert turn(workspace).summary.queued == 0

    append_event(workspace.journal, LAUNCHED, definition="evalset.py", tasks=1)

    assert turn(workspace).summary.queued == 1


def test_a_later_ruling_about_something_else_does_not_re_latch_the_task(
    tmp_path: Path,
) -> None:
    """Each task is held by the decision that accepted *it*.

    Asking whether *any* settled window postdates the launch and then latching
    every accepted identifier gives the same answer nearly always — and after a
    relaunch it silently stops the respawns under an `exclude` about a sample
    error, which said nothing about whether the task should run again.
    """
    task = SynthTask("probe", samples=4)
    workspace, _ = prepared(tmp_path, [task])
    write_log(
        workspace.logs,
        task,
        format="eval",
        total=2,
        completed=2,
        error="ScorerError('no grade')",
        error_traceback=SCORER_TRACEBACK,
        samples=[errored("s0")],
    )
    append_event(workspace.journal, PAUSED, by="test", reason="hold the respawn")
    turn(workspace)
    accept_the_failure(workspace)
    assert turn(workspace).summary.queued == 0

    append_event(workspace.journal, LAUNCHED, definition="evalset.py", tasks=1)
    assert turn(workspace).summary.queued == 1, "the premise: the launch released it"

    unrelated: dict[str, Any] = {
        "class": SAMPLE_CLASS,
        "disposition": "exclude",
        "reason": "the provider was down; this one is not coming back",
        "by": "kaia",
        "effect": "1 sample excluded from scoring",
    }
    append_event(workspace.journal, RULING, **unrelated)

    assert turn(workspace).summary.queued == 1
