"""The tuning policy, as a table.

Pure throughout, which is the point of the module under test: every gate, the storm cut, the restore, and the budget arithmetic are driven with hand-built signals and no live worker — and that is not a convenience but the only coverage the ramp-down path can ever have, because `mockllm` cannot produce a rate-limit episode (design/testing.md).

The asymmetry gets the most cases, because it is the design's central promise: a step up is bought with a whole clean window and a cut is bought with nothing — holds included.
"""

from pathlib import Path
from typing import Any

import pytest
from inspect_steward._evalset.observe import ObservedTasks
from inspect_steward._schedule import InFlight, Summary
from inspect_steward._tend import (
    TUNING_PROPOSAL,
    Baseline,
    Level,
    Move,
    Owner,
    TaskSignals,
    TendResult,
    TuningPlan,
    observation_payload,
    plan_tuning,
    read_baseline,
    read_ramp_record,
)
from inspect_steward._tend.items import tend_items
from inspect_steward._tend.tuning import (
    CONNECTIONS_FLOOR,
    CPU_GATE,
    RETRY_GATE,
    STEP_SPACING,
)
from inspect_steward._tend.turn import _Acted, _retune
from inspect_steward._worker import ConfigView
from inspect_steward._workspace import (
    ACTION,
    OBSERVATION,
    RampHold,
    append_event,
    create_workspace,
    read_journal,
)

EDGE = 1000.0
"""When the previous observation was written."""

NOW = 1600.0
"""This turn's clock: a ten-minute window since `EDGE`."""

RAMP = (40, 200)


def sig(
    identifier: str = "t1",
    *,
    level: int | None = 40,
    in_use: int = 40,
    pid: int = 1,
    errored: int = 0,
    retries: int = 0,
    scale_downs: tuple[float, ...] = (),
    sandboxes: tuple[int, int] | None = None,
    ceiling: int | None = None,
    limit: int | None = None,
) -> TaskSignals:
    """One task's window, defaulting to a clean, saturated one."""
    return TaskSignals(
        identifier=identifier,
        key=identifier,
        task_id=f"T-{identifier}",
        pid=pid,
        level=level,
        in_use=in_use,
        errored=errored,
        http_retries=retries,
        scale_downs=scale_downs,
        sandboxes=sandboxes,
        connections_ceiling=ceiling,
        connections_limit=limit,
    )


def base(
    *identifiers: str,
    level: int = 40,
    pids: tuple[int, ...] = (1,),
    pushback: tuple[str, ...] = (),
    capacity: tuple[str, ...] = (),
) -> Baseline:
    """The previous turn's record for these tasks, matching `sig`'s defaults."""
    names = identifiers or ("t1",)
    return Baseline(
        ts=EDGE,
        levels={name: level for name in names},
        cpu={pid: 10.0 for pid in pids},
        retries={name: 0 for name in names},
        errors={name: 0 for name in names},
        pushback=frozenset(pushback),
        capacity=frozenset(capacity),
    )


def plan(
    *tasks: TaskSignals,
    ramp: tuple[int, int] | None = RAMP,
    budget: int | None = None,
    baseline: Baseline | None = None,
    holds: dict[str, RampHold] | None = None,
    last_step: dict[str, float] | None = None,
    cpu: dict[int, float] | None = None,
    absent: tuple[str, ...] = (),
) -> TuningPlan:
    return plan_tuning(
        list(tasks) or [sig()],
        ramp=ramp,
        budget=budget,
        baseline=baseline if baseline is not None else base(),
        holds=holds or {},
        last_step=last_step or {},
        cpu=cpu if cpu is not None else {1: 12.0},
        now=NOW,
        absent=absent,
    )


def steps(result: TuningPlan) -> list[Move]:
    return [move for move in result.moves if move.knob == "max_samples"]


def ceilings(result: TuningPlan) -> list[Move]:
    return [move for move in result.moves if move.knob == "max_connections"]


def hold(identifier: str = "") -> dict[str, RampHold]:
    return {
        identifier: RampHold(
            by="agent", reason="anomalies rising", ts="", identifier=identifier
        )
    }


# --- the up-gate: a step is bought with a whole clean window --------------


def test_a_clean_saturated_window_buys_one_step() -> None:
    (move,) = steps(plan())

    assert (move.at, move.to) == (40, 60)
    assert "authorized range 40–200" in move.reason


def test_headroom_nobody_is_using_is_not_capacity() -> None:
    # saturation measures demand; raising an unsaturated limiter reports as
    # discovered capacity what was never asked for
    assert steps(plan(sig(in_use=39))) == []


def test_pushback_this_window_blocks_the_step() -> None:
    result = plan(sig(scale_downs=(EDGE + 200,)))

    assert steps(result) == []
    assert result.record["pushback"] == ["t1"]


def test_pushback_before_the_window_does_not() -> None:
    (move,) = steps(plan(sig(scale_downs=(EDGE - 100,))))

    assert move.to == 60


def test_surging_retries_block_the_step() -> None:
    # a quarter of a retry per sample slot: clear of the transient 5xxs any
    # long-running endpoint produces, well under the one-per-slot that would
    # let a materially unhealthy window read as clean
    assert steps(plan(sig(retries=int(40 * RETRY_GATE) + 1))) == []
    assert steps(plan(sig(retries=int(40 * RETRY_GATE)))) != []


def test_new_sample_errors_block_the_step() -> None:
    # don't accelerate an arm that is breaking samples; the agent still owns
    # interpreting the errors themselves
    assert steps(plan(sig(errored=1))) == []


def test_a_counter_that_went_backwards_is_a_respawn_not_a_quiet_window() -> None:
    baseline = Baseline(
        ts=EDGE, levels={"t1": 40}, cpu={1: 10.0}, retries={"t1": 5}, errors={"t1": 0}
    )

    assert steps(plan(sig(retries=0), baseline=baseline)) == []


def test_a_hot_worker_blocks_its_own_step() -> None:
    burned = 10.0 + CPU_GATE * (NOW - EDGE)

    assert steps(plan(cpu={1: burned})) == []
    assert steps(plan(cpu={1: burned - 1.0})) != []


def test_a_worker_with_no_cpu_baseline_waits() -> None:
    # unknown is not clean: a respawned worker's new pid has no previous reading
    assert steps(plan(cpu={2: 12.0})) == []


def test_a_level_that_moved_this_window_measured_nothing() -> None:
    # somebody — the loop itself last turn, or a person over the control
    # channel — changed the setpoint mid-window, so the window says nothing
    # about the level it ends at
    assert steps(plan(sig(level=60, in_use=60))) == []


def test_the_first_window_never_steps() -> None:
    assert steps(plan(baseline=Baseline())) == []


def test_a_recent_step_lets_the_batch_settle() -> None:
    # a +20 batch has sandboxes to start; a window read before the new level is
    # exercised would measure startup and call it clean
    recent = {"t1": NOW - STEP_SPACING + 60}
    spaced = {"t1": NOW - STEP_SPACING - 60}

    assert steps(plan(last_step=recent)) == []
    assert steps(plan(last_step=spaced)) != []


def test_a_step_never_crosses_the_ceiling() -> None:
    (move,) = steps(plan(sig(level=190, in_use=190), baseline=base(level=190)))

    assert move.to == 200


def test_a_hold_freezes_the_climb() -> None:
    fleet = plan(holds=hold())

    assert steps(fleet) == []
    assert any("held by agent" in line for line in fleet.lines)


def test_a_hold_on_one_arm_leaves_the_others_climbing() -> None:
    result = plan(
        sig("t1"),
        sig("t2", pid=2),
        baseline=base("t1", "t2", pids=(1, 2)),
        holds=hold("t1"),
        cpu={1: 12.0, 2: 12.0},
    )

    assert [move.identifier for move in steps(result)] == ["t2"]


# --- the machine-wide sandbox budget --------------------------------------


def test_the_budget_caps_the_fleet_wide_sum() -> None:
    # 40 + 40 committed against 100: one +20 step fits, the second does not
    result = plan(
        sig("t1", sandboxes=(30, 100)),
        sig("t2", pid=2, sandboxes=(30, 100)),
        budget=100,
        baseline=base("t1", "t2", pids=(1, 2)),
        cpu={1: 12.0, 2: 12.0},
    )

    assert [move.identifier for move in steps(result)] == ["t1"]
    assert any("sandbox budget" in line for line in result.lines)


def test_the_budget_falls_back_to_what_the_workers_report() -> None:
    # no declared max_sandboxes: the provider default the process reports is a
    # statement about the host, read back as the machine's budget
    result = plan(sig(sandboxes=(30, 50)), budget=None)

    assert steps(result) == []
    assert any("sandbox budget (40/50)" in line for line in result.lines)


def test_a_floor_already_over_the_budget_never_steps() -> None:
    result = plan(sig(sandboxes=(30, 30)), budget=30)

    assert steps(result) == []


def test_a_worker_that_did_not_answer_still_holds_its_share() -> None:
    # busy is the ordinary state of a fleet mid-generate, not a failure: those
    # samples are running whether or not the socket answered in two seconds,
    # and leaving them out of the sum is how the machine gets over-committed
    # 40 answering against a budget of 90 leaves room for a step; the busy
    # sibling's own 40 is what takes it away
    answering = sig("t1", sandboxes=(30, 90))
    baseline = base("t1", "t2", pids=(1,))

    alone = plan(answering, budget=90, baseline=baseline)
    withheld = plan(answering, budget=90, baseline=baseline, absent=("t2",))

    assert steps(alone) != []
    assert steps(withheld) == []
    assert any("on workers that did not answer" in line for line in withheld.lines)


def test_an_unread_worker_nobody_has_a_level_for_is_charged_the_floor() -> None:
    # a task spawned since the last observation has no recorded level, and the
    # ramp floor is the least it can be running
    running = sig("t1", level=150, in_use=150, sandboxes=(30, 200))
    baseline = base(level=150)

    assert steps(plan(running, budget=200, baseline=baseline)) != []
    assert steps(plan(running, budget=200, baseline=baseline, absent=("fresh",))) == []


def test_the_budget_is_charged_what_the_step_costs_not_a_whole_one() -> None:
    # a task finishing its climb asks for less than a full step, and refusing
    # a move that fits would strand it one short of a bound both numbers allow
    (move,) = steps(
        plan(
            sig(level=190, in_use=190, sandboxes=(150, 200)),
            budget=200,
            baseline=base(level=190),
        )
    )

    assert (move.at, move.to) == (190, 200)


def test_an_elastic_provider_caps_nothing() -> None:
    # no sandbox limiter — k8s, or no sandbox at all — and the budget is not
    # this task's concern even when one is declared
    (move,) = steps(plan(sig(sandboxes=None), budget=30))

    assert move.to == 60


# --- the storm: down is bought with nothing -------------------------------


def storm() -> TaskSignals:
    """Pushback this window, on a task whose previous window already had some."""
    return sig(level=60, in_use=60, scale_downs=(EDGE + 200,), ceiling=200, limit=37)


def test_sustained_pushback_cuts_the_ceiling_and_steps_down() -> None:
    result = plan(storm(), baseline=base(level=60, pushback=("t1",)))

    (cut,) = ceilings(result)
    assert (cut.at, cut.to) == (200, 37)
    assert "retry storm" in cut.reason
    (down,) = steps(result)
    assert (down.at, down.to) == (60, 40)


def test_one_episode_is_the_controllers_job_not_a_storm() -> None:
    # a single window's scale-downs are AIMD working; tend cuts only when
    # independent controllers keep probing back into the same wall
    result = plan(storm(), baseline=base(level=60))

    assert result.moves == []


def test_the_cut_ignores_holds() -> None:
    # the cut exists precisely for when nobody is watching; a hold is a brake
    # on growth, never on safety
    result = plan(storm(), baseline=base(level=60, pushback=("t1",)), holds=hold())

    assert len(ceilings(result)) == 1
    assert len(steps(result)) == 1


def test_a_storm_at_the_floor_cuts_connections_only() -> None:
    result = plan(
        sig(scale_downs=(EDGE + 200,), ceiling=200, limit=37),
        baseline=base(pushback=("t1",)),
    )

    assert len(ceilings(result)) == 1
    assert steps(result) == []


def test_the_cut_clamps_to_the_highest_controller_not_their_total() -> None:
    # the ceiling is one number worn by every controller in the process, so a
    # sum would bound each of them at what all of them together were using --
    # which is not a cut at all. Two packed rows sitting at 37 and 20 get 37
    result = plan(
        storm(),
        sig(
            "t2", level=60, in_use=60, scale_downs=(EDGE + 200,), ceiling=200, limit=20
        ),
        baseline=base("t1", "t2", level=60, pushback=("t1", "t2")),
    )

    (cut,) = ceilings(result)
    assert cut.to == 37


def test_the_cut_never_goes_below_the_controllers_start() -> None:
    result = plan(
        sig(scale_downs=(EDGE + 200,), ceiling=200, limit=4),
        baseline=base(pushback=("t1",)),
    )

    (cut,) = ceilings(result)
    assert cut.to == CONNECTIONS_FLOOR


# --- the way back up is stepwise ------------------------------------------


def test_a_clear_window_restores_the_ceiling_by_doubling() -> None:
    (raise_,) = ceilings(plan(sig(ceiling=50, limit=30)))

    assert (raise_.at, raise_.to) == (50, 100)


def test_the_restore_stops_at_the_ramp_ceiling() -> None:
    (raise_,) = ceilings(plan(sig(ceiling=150, limit=30)))

    assert raise_.to == 200


def test_a_fresh_worker_gets_the_ceiling_before_it_has_a_window() -> None:
    # the one move that does not wait for a baseline: until the ceiling moves,
    # the default bound silently caps a climb the range authorized
    result = plan(sig(ceiling=100), baseline=Baseline())

    (raise_,) = ceilings(result)
    assert raise_.to == 200
    assert steps(result) == []


def test_pushback_or_a_hold_stalls_the_restore() -> None:
    pushing = plan(sig(ceiling=50, scale_downs=(EDGE + 200,)))
    held = plan(sig(ceiling=50), holds=hold())

    assert ceilings(pushing) == []
    assert ceilings(held) == []


def test_holding_one_arm_holds_its_process_s_ceiling_too() -> None:
    # the knob is process-scoped, so it cannot be raised for one task and not
    # its sibling: the only reading that keeps `ramp hold <identifier>` honest
    # is the one that treats a held row as holding the process
    alone = plan(sig(ceiling=50), holds=hold("t1"))
    packed = plan(
        sig(ceiling=50),
        sig("t2", ceiling=50),
        baseline=base("t1", "t2"),
        holds=hold("t2"),
    )

    assert ceilings(alone) == []
    assert ceilings(packed) == []


def test_a_ceiling_already_at_target_is_left_alone() -> None:
    assert ceilings(plan(sig(ceiling=200))) == []


# --- pinned mode: the signal runs, the authority does not -----------------


def test_a_pinned_setpoint_is_never_moved() -> None:
    result = plan(sig(ceiling=100), ramp=None)

    assert result.moves == []
    assert not result.active


def test_pinned_capacity_becomes_a_proposal_on_the_second_window() -> None:
    first = plan(ramp=None)
    second = plan(ramp=None, baseline=base(capacity=("t1",)))

    assert first.proposals == [] and first.record["capacity"] == ["t1"]
    (proposal,) = second.proposals
    assert proposal.pinned and proposal.level == 40


def test_a_ramp_at_its_ceiling_proposes_raising_the_envelope() -> None:
    at_top = sig(level=200, in_use=200)

    first = plan(at_top, baseline=base(level=200))
    second = plan(at_top, baseline=base(level=200, capacity=("t1",)))

    assert first.proposals == []
    (proposal,) = second.proposals
    assert not proposal.pinned and proposal.ceiling == 200


def test_narrowing_the_range_brings_a_running_task_back_inside_it() -> None:
    # the envelope is the authorization, so a level outside it is not capacity
    # being declined but authority already exceeded -- and the task must not
    # instead report itself as sitting happily at a ceiling it is above
    over = sig(level=200, in_use=200)
    result = plan(over, ramp=(40, 100), baseline=base(level=200, capacity=("t1",)))

    (move,) = steps(result)
    assert (move.at, move.to) == (200, 100)
    assert "the authorized range now ends at 100" in move.reason
    assert result.proposals == []


def test_the_correction_is_not_gated_like_a_step() -> None:
    # no clean window, no spacing, and no deference to a hold: holding brakes
    # growth, and this is the envelope being enforced rather than spent
    over = sig(level=200, in_use=140, errored=3, scale_downs=(EDGE + 200,))
    result = plan(
        over,
        ramp=(40, 100),
        baseline=base(level=200),
        holds=hold(),
        last_step={"t1": NOW},
    )

    (move,) = steps(result)
    assert move.to == 100


def test_a_dirty_window_is_not_capacity() -> None:
    result = plan(sig(errored=1), ramp=None, baseline=base(capacity=("t1",)))

    assert result.proposals == [] and result.record["capacity"] == []


def test_a_held_task_is_not_offered_as_capacity() -> None:
    # somebody said stop; proposing more of what they stopped is not listening
    result = plan(ramp=None, baseline=base(capacity=("t1",)), holds=hold())

    assert result.proposals == []


# --- what the turn writes down --------------------------------------------


def test_the_record_carries_what_the_turn_left_behind() -> None:
    result = plan()
    (move,) = steps(result)

    applied = observation_payload(result, [move])
    skipped = observation_payload(result, [])

    assert applied["levels"] == {"t1": 60}
    assert skipped["levels"] == {"t1": 40}
    assert applied["cpu"] == {"1": 12.0}


def test_the_baseline_reads_back_what_the_record_wrote(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    result = plan()
    append_event(
        journal, OBSERVATION, tuning=observation_payload(result, steps(result))
    )

    baseline = read_baseline(read_journal(journal).events)

    assert baseline.ts is not None
    assert baseline.levels == {"t1": 60}
    assert baseline.cpu == {1: 12.0}
    assert baseline.retries == {"t1": 0}


def test_an_observation_without_a_record_is_not_a_window(tmp_path: Path) -> None:
    # an older turn's observation says nothing this one can be a delta against
    journal = tmp_path / "journal.jsonl"
    append_event(journal, OBSERVATION, running=2)

    assert read_baseline(read_journal(journal).events).ts is None


def test_the_ramp_record_folds_to_levels_and_step_times(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    append_event(
        journal, ACTION, action="ramp", knob="max_samples", identifier="t1", to=60
    )
    append_event(
        journal, ACTION, action="ramp", knob="max_connections", identifier="t1", to=200
    )
    append_event(
        journal, ACTION, action="ramp", knob="max_samples", identifier="t1", to=80
    )

    levels, last_step = read_ramp_record(read_journal(journal).events)

    # the connections move is not a level, and the last word wins
    assert levels == {"t1": 80}
    assert set(last_step) == {"t1"}


# --- the proposal as an item ----------------------------------------------


def test_a_proposal_is_the_humans_item_and_its_id_carries_the_level() -> None:
    """Capacity at 60 accepted is not capacity at 80 accepted.

    The item is acknowledgeable — the agent relays it and records the ruling
    with `steward ack` — and the level in the id is what keeps that ruling
    narrow: a task later authorized higher produces a fresh item the first time
    it holds a clean window at its new bound.
    """
    result = TendResult(
        summary=Summary(
            tasks=1,
            states={},
            reasons={},
            running=1,
            workers=1,
            spawning=0,
            spawning_workers=0,
            queued=0,
            stalled=[],
            orphans=[],
            orphans_running=[],
            archiving=0,
            unreadable=0,
            max_workers=None,
            max_tasks=None,
            blocked=None,
            capture_rss=None,
            paused=False,
        ),
        queued=[],
        drift=False,
        degraded=None,
        claim=None,
        broke=None,
        tuning=plan(ramp=None, baseline=base(capacity=("t1",))),
    )

    items = tend_items(result, ObservedTasks(tasks=[]), InFlight())

    (item,) = [entry for entry in items if entry.kind == TUNING_PROPOSAL]
    assert item.owner is Owner.HUMAN
    assert item.level is Level.INFO
    assert item.acknowledgeable
    assert item.id.endswith(":40")
    assert "pinned" in item.summary


# --- carrying out a move, and the receipt for it --------------------------


def retune(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: ConfigView
) -> tuple[list[Move], list[str]]:
    """One move executed against a control channel that answers `outcome`."""
    workspace = create_workspace(tmp_path, git=False).workspace

    def answered(task_id: str, **knobs: Any) -> ConfigView:
        return outcome

    monkeypatch.setattr("inspect_steward._tend.turn.task_config", answered)
    move = Move(
        identifier="t1",
        key="t1",
        task_id="T-t1",
        knob="max_samples",
        at=40,
        to=60,
        reason="clean window",
    )
    acted = _Acted()
    applied = _retune(workspace, TuningPlan(moves=[move]), acted)
    return applied, acted.failures


def test_a_retune_the_eval_log_did_not_record_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # the change is live and stays live -- undoing something that worked
    # because its receipt was not filed would be the wrong repair -- but one of
    # the three records the ramp promises is missing, and an unattended retune
    # nobody can find afterwards is what provenance exists to prevent
    applied, failures = retune(
        monkeypatch,
        tmp_path,
        ConfigView(applied=True, persisted={"max_samples": False}),
    )

    assert len(applied) == 1
    (failure,) = failures
    assert "not recorded in the eval log" in failure


def test_a_retune_recorded_everywhere_says_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    applied, failures = retune(
        monkeypatch,
        tmp_path,
        ConfigView(applied=True, persisted={"max_samples": True}),
    )

    assert len(applied) == 1 and failures == []
