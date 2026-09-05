"""The rehearsal leaves out what the log store already answers.

Rung 2 of the convergence ladder, consulted by the smoke with the launch's own predicate: a task the launch will copy in is not work the run does, so it is not work the rehearsal does either. The record still names every task the capture enumerated, so the launch gate's subset rule holds. Layer 1 throughout: a directory store of synthesized logs, a stubbed fleet, no processes.
"""

import importlib
from pathlib import Path

import pytest
from inspect_steward._evalset.manifest import manifest_digest
from inspect_steward._evalset.observe import observe_logs
from inspect_steward._smoke import SCAN_COVERAGE, Outcome
from inspect_steward._smoke import run as run_module
from inspect_steward._smoke.digest import digest_markdown, journal_fields
from inspect_steward._smoke.run import (
    Plan,
    conclude,
    divide,
    prepare,
    settled,
    smoke,
    unfinished,
)
from inspect_steward._store import StoreError
from inspect_steward._workspace import (
    SMOKED,
    Held,
    Workspace,
    create_workspace,
    read_journal,
    read_smoked,
)

from .._logs import SynthTask, synth_manifest, write_log
from ..launch._fake import fake_capture

ADDITION = SynthTask("addition", samples=2)
ECHO = SynthTask("echo", samples=2)


def stored(tmp_path: Path, *tasks: SynthTask) -> Path:
    location = tmp_path / "store"
    for task in tasks:
        write_log(location, task)
    return location


def planned(tmp_path: Path, *satisfied: SynthTask) -> Plan:
    """Both tasks captured, `satisfied` answered by a store."""
    create_workspace(tmp_path, git=False)
    return prepare(
        Workspace.at(tmp_path),
        synth_manifest([ADDITION, ECHO]),
        cap=0,
        satisfied={task.identifier: f"store/{task.name}.json" for task in satisfied},
        store=str(tmp_path / "store"),
    )


class Fleet:
    """A spawn that starts nothing and keeps the plan it was handed."""

    def __init__(self) -> None:
        self.plans: list[Plan] = []

    def spawn(
        self,
        workspace: Workspace,
        definition: Path,
        plan: Plan,
        *,
        deadline: float | None = None,
    ) -> list[str]:
        self.plans.append(plan)
        return []


def stubbed(monkeypatch: pytest.MonkeyPatch) -> Fleet:
    fleet = Fleet()

    def watch(plan: Plan, *, now: float) -> bool:
        return False

    monkeypatch.setattr(run_module, "spawn", fleet.spawn)
    monkeypatch.setattr(run_module, "watch", watch)
    return fleet


def rehearse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: object
) -> tuple[Workspace, Fleet]:
    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    fake_capture(monkeypatch, synth_manifest([ADDITION, ECHO]))
    fleet = stubbed(monkeypatch)
    result = smoke(workspace, tmp_path / "evalset.py", cap=0, **kwargs)  # type: ignore[arg-type]
    assert not isinstance(result, Held)
    return workspace, fleet


class TestWhatThePlanRuns:
    def test_a_satisfied_task_is_dealt_to_no_process(self, tmp_path: Path) -> None:
        plan = planned(tmp_path, ADDITION)

        dealt = [task.identifier for share in divide(plan) for task in share]

        assert dealt == [ECHO.identifier]

    def test_the_manifest_stays_whole_and_rehearsed_is_the_rest(
        self, tmp_path: Path
    ) -> None:
        plan = planned(tmp_path, ADDITION)

        assert [task.identifier for task in plan.manifest.tasks] == [
            ADDITION.identifier,
            ECHO.identifier,
        ]
        assert [task.identifier for task in plan.rehearsed.tasks] == [ECHO.identifier]

    def test_the_watch_and_the_checks_wait_for_nothing_the_store_answers(
        self, tmp_path: Path
    ) -> None:
        # a satisfied task never writes a log into the rehearsal's directory,
        # so measured against the whole manifest the watch would spin to the
        # cap and `unfinished` would name it as having produced nothing
        plan = planned(tmp_path, ADDITION)
        write_log(Path(plan.log_dir), ECHO)
        logs = observe_logs(plan.log_dir)

        assert settled(logs, plan.rehearsed)
        assert unfinished(plan.rehearsed, logs) == []
        assert not settled(logs, plan.manifest)


class TestWhatTheDigestSays:
    def test_a_partial_rehearsal_records_everything_and_ran_the_rest(
        self, tmp_path: Path
    ) -> None:
        plan = planned(tmp_path, ADDITION)
        write_log(Path(plan.log_dir), ECHO)

        result = conclude(
            plan,
            logs=observe_logs(plan.log_dir),
            capped=False,
            elapsed=1.0,
            waived=(SCAN_COVERAGE,),
        )

        assert result.tasks == 1
        assert set(result.identifiers) == {ADDITION.identifier, ECHO.identifier}
        assert result.digest == manifest_digest(plan.manifest)
        assert [one.identifier for one in result.satisfied] == [ADDITION.identifier]
        assert result.population == 4
        rendered = digest_markdown(result)
        assert "1 task rehearsed" in rendered
        assert "## satisfied from the log store" in rendered
        assert "store/addition.json" in rendered

    def test_nothing_to_rehearse_passes_with_no_check_asked(
        self, tmp_path: Path
    ) -> None:
        plan = planned(tmp_path, ADDITION, ECHO)

        result = conclude(
            plan, logs=observe_logs(plan.log_dir), capped=False, elapsed=0.0, waived=()
        )

        assert result.outcome is Outcome.PASSED
        assert result.tasks == 0 and result.landed == 0
        assert result.probe.checks == ()
        assert result.errors == ()
        assert set(result.identifiers) == {ADDITION.identifier, ECHO.identifier}
        verdict = digest_markdown(result).splitlines()[2]
        assert "nothing to rehearse" in verdict
        assert str(tmp_path / "store") in verdict
        fields = journal_fields(result)
        assert set(fields["satisfied"]) == {ADDITION.identifier, ECHO.identifier}
        assert fields["log_store"] == str(tmp_path / "store")

    def test_an_empty_capture_is_still_a_failure(self, tmp_path: Path) -> None:
        # a capture that enumerated nothing has nothing the store could answer,
        # and blessing it as rehearsed is the case the guard exists for
        create_workspace(tmp_path, git=False)
        plan = prepare(Workspace.at(tmp_path), synth_manifest([]), cap=0)

        result = conclude(
            plan, logs=observe_logs(plan.log_dir), capped=False, elapsed=0.0, waived=()
        )

        assert result.outcome is Outcome.FAILED


class TestTheRehearsalEndToEnd:
    def test_every_task_satisfied_starts_nothing_and_records_why(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = stored(tmp_path, ADDITION, ECHO)

        workspace, fleet = rehearse(tmp_path, monkeypatch, log_store=str(store))

        assert fleet.plans == []
        record = read_smoked(read_journal(workspace.journal).events)
        assert record.identifiers == {ADDITION.identifier, ECHO.identifier}
        assert record.satisfied == {ADDITION.identifier, ECHO.identifier}
        assert (workspace.smoke / "digest.md").exists()

    def test_the_fleet_is_handed_only_what_the_store_does_not_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = stored(tmp_path, ADDITION)

        _, fleet = rehearse(tmp_path, monkeypatch, log_store=str(store))

        (plan,) = fleet.plans
        assert [task.identifier for task in plan.rehearsed.tasks] == [ECHO.identifier]

    def test_declining_the_store_rehearses_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = stored(tmp_path, ADDITION, ECHO)
        create_workspace(tmp_path, git=False)
        (tmp_path / "_steward.yaml").write_text(
            f"log_store: {store}\n", encoding="utf-8"
        )

        _, fleet = rehearse(tmp_path, monkeypatch, log_store=False)

        (plan,) = fleet.plans
        assert len(plan.rehearsed.tasks) == 2

    def test_the_file_is_honoured_where_nothing_is_said(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = stored(tmp_path, ADDITION)
        create_workspace(tmp_path, git=False)
        (tmp_path / "_steward.yaml").write_text(
            f"log_store: {store}\n", encoding="utf-8"
        )

        _, fleet = rehearse(tmp_path, monkeypatch)

        (plan,) = fleet.plans
        assert [task.identifier for task in plan.rehearsed.tasks] == [ECHO.identifier]

    def test_a_log_short_of_the_run_is_rehearsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = tmp_path / "store"
        write_log(store, ADDITION, total=1, completed=1)

        _, fleet = rehearse(tmp_path, monkeypatch, log_store=str(store))

        (plan,) = fleet.plans
        assert len(plan.rehearsed.tasks) == 2

    def test_a_store_that_will_not_open_costs_one_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # a store never fails a launch, and it must not fail the smoke that
        # precedes one: everything is rehearsed, and the log says why
        module = importlib.import_module("inspect_steward._store.match")

        def refuse(location: str, *, root: Path) -> object:
            raise StoreError("the bucket is not there")

        monkeypatch.setattr(module, "open_store", refuse)

        workspace, fleet = rehearse(
            tmp_path, monkeypatch, log_store=str(tmp_path / "store")
        )

        (plan,) = fleet.plans
        assert len(plan.rehearsed.tasks) == 2
        assert "nothing could be reused" in workspace.log.read_text(encoding="utf-8")
        assert all(
            event.payload.get("satisfied") == []
            for event in read_journal(workspace.journal).events
            if event.type == SMOKED
        )
