"""Nothing this rehearsal started outlives it.

**Untended is what makes a smoke cheap and what makes a leak permanent.** Nothing reconciles a rehearsal, so a worker left running by an exception on the third task of five is a worker nobody will ever stop: it holds its sandboxes and spends its tokens against a smoke that has already printed its verdict. Three ways out of a rehearsal — the cap, an exception, an interrupt — and all three go through the same door.

And the cap itself has a second half. A cancel returns as soon as `inspect ctl` accepts it, while the worker is still finalizing its log and writing its scan rows, so a capped rehearsal that read the directory in the next breath under-reported what it ran and folded a scan mid-write.
"""

from pathlib import Path
from typing import Any

import pytest
from inspect_steward._evalset.observe import observe_logs
from inspect_steward._launch import LaunchError
from inspect_steward._smoke import Outcome, Smoke
from inspect_steward._smoke import run as run_module
from inspect_steward._smoke.run import Plan, conclude, divide, prepare, smoke, watch
from inspect_steward._workspace import (
    Workspace,
    create_workspace,
    read_journal,
    read_smoked,
)

from .._logs import SynthTask, synth_manifest, write_log
from ..launch._fake import fake_capture

ADDITION = SynthTask("addition", samples=2)
ECHO = SynthTask("echo", samples=1)


class Reaper:
    """A stand-in for the one exit, recording who it was asked to take down."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, workspace: Workspace, workers: Any) -> None:
        self.calls.append(list(workers))


class Spawned:
    """What `Fleet.spawn` hands back — only the stem is read here."""

    def __init__(self, worker: str) -> None:
        self.worker = worker


class BrokenFleet:
    """A fleet that starts one worker and then cannot start the next."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.started = 0

    def spawn(self, action: Any) -> Spawned:
        self.started += 1
        if self.started > 1:
            raise OSError("no more processes")
        return Spawned(f"worker-{self.started}")


def planned(tmp_path: Path) -> tuple[Workspace, Plan]:
    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    return workspace, prepare(workspace, synth_manifest([ADDITION, ECHO]), cap=0)


class TestTheCapDoesNotStopAnything:
    """Watching and stopping came apart, so that every exit uses the stopping half."""

    def test_watching_a_settled_directory_returns_without_a_cap(
        self, tmp_path: Path
    ) -> None:
        _, plan = planned(tmp_path)
        for task in plan.manifest.tasks:
            _land(Path(plan.log_dir), task.name)

        assert watch(plan, now=0.0) is False

    def test_a_deadline_already_past_reports_the_cap(self, tmp_path: Path) -> None:
        _, plan = planned(tmp_path)

        assert watch(_capped(plan), now=-3600.0) is True


class TestNothingIsLeftRunning:
    def test_a_spawn_that_fails_halfway_takes_back_what_it_started(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # only `spawn` knows what it started when it failed, which is why the
        # cleanup is there rather than in the caller's `finally`
        workspace, plan = planned(tmp_path)
        reaper = Reaper()
        monkeypatch.setattr(run_module, "Fleet", BrokenFleet)
        monkeypatch.setattr(run_module, "reap", reaper)

        with pytest.raises(OSError):
            run_module.spawn(workspace, tmp_path / "evalset.py", plan)

        assert reaper.calls == [["worker-1"]]

    def test_a_rehearsal_that_raises_mid_watch_still_reaps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the reason it is a `finally` rather than a line after the watch: an
        # interrupt at minute nine leaves a fleet nothing will ever reconcile
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        fake_capture(monkeypatch, synth_manifest([ADDITION, ECHO]))
        reaper = Reaper()

        def spawn(
            workspace: Workspace,
            definition: Path,
            plan: Plan,
            *,
            deadline: float | None = None,
        ) -> list[str]:
            return ["worker-1", "worker-2"]

        def watch(plan: Plan, *, now: float) -> bool:
            raise KeyboardInterrupt

        monkeypatch.setattr(run_module, "spawn", spawn)
        monkeypatch.setattr(run_module, "watch", watch)
        monkeypatch.setattr(run_module, "reap", reaper)

        with pytest.raises(KeyboardInterrupt):
            smoke(workspace, tmp_path / "evalset.py", cap=0)

        assert reaper.calls == [["worker-1", "worker-2"]]

    def test_reaping_nothing_reads_no_process_table(self, tmp_path: Path) -> None:
        # the settled path, which is the common one: a rehearsal whose tasks all
        # landed has nothing to stop, and must not pay to find that out twice
        workspace, _ = planned(tmp_path)

        run_module.reap(workspace, [])

        assert not workspace.smoke_inflight.exists()


class TestHowManyProcessesItTakes:
    """`max_workers` is the run's shape, and a rehearsal has to have it too.

    Left out, a fifty-task definition constrained to four processes rehearsed in fifty: not the run, a burst of startup cost the operator had already declined, and on a machine picked for four a rehearsal that fails for a reason the launch would not have hit.
    """

    def dealt(self, tmp_path: Path, tasks: int, workers: int | None) -> list[int]:
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        plan = prepare(
            workspace,
            synth_manifest([SynthTask(f"task{one}") for one in range(tasks)]),
            max_workers=workers,
        )
        return [len(share) for share in divide(plan)]

    def test_one_process_per_task_when_nothing_bounds_it(self, tmp_path: Path) -> None:
        assert self.dealt(tmp_path, tasks=5, workers=None) == [1, 1, 1, 1, 1]

    def test_the_pool_the_launch_is_allowed(self, tmp_path: Path) -> None:
        assert self.dealt(tmp_path, tasks=5, workers=2) == [3, 2]

    def test_a_pool_wider_than_the_work_starts_only_the_work(
        self, tmp_path: Path
    ) -> None:
        assert self.dealt(tmp_path, tasks=2, workers=8) == [1, 1]

    def test_nothing_to_run_starts_nothing(self, tmp_path: Path) -> None:
        assert self.dealt(tmp_path, tasks=0, workers=4) == []


class TestClearingTheLastRehearsal:
    def test_a_clearing_that_fails_stops_the_rehearsal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `ignore_errors` was actively dangerous here rather than merely lax: a
        # surviving log satisfies `settled` before a worker has written anything
        # and lands in the digest as this run's evidence
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        workspace.smoke.mkdir(parents=True)

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise OSError("device or resource busy")

        monkeypatch.setattr(run_module, "rmtree", refuse)

        with pytest.raises(LaunchError, match="could not be cleared"):
            prepare(workspace, synth_manifest([ADDITION]))


class TestABadScannerIsARefusal:
    def test_a_name_colliding_with_the_built_in_is_a_message_not_a_traceback(
        self, tmp_path: Path
    ) -> None:
        # the same refusal `_launch` turns this into, arriving through the same
        # exception the CLI already prints
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)

        with pytest.raises(LaunchError, match="collides"):
            prepare(
                workspace,
                synth_manifest([ADDITION]),
                scanners={"scoring_integrity": {"name": "pkg/other"}},
            )

    def test_a_malformed_reference_is_too(self, tmp_path: Path) -> None:
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)

        with pytest.raises(LaunchError, match="not a valid scanner reference"):
            prepare(
                workspace,
                synth_manifest([ADDITION]),
                scanners={"mine": {"nome": "typo"}},
            )


def _capped(plan: Plan) -> Plan:
    """The same rehearsal under a one-minute deadline."""
    return Plan(
        manifest=plan.manifest,
        log_dir=plan.log_dir,
        scan_id=plan.scan_id,
        scan_dir=plan.scan_dir,
        scanners=plan.scanners,
        samples=plan.samples,
        cap=1,
    )


def _land(log_dir: Path, name: str) -> None:
    """A settled log for one of the planned tasks."""
    write_log(log_dir, ADDITION if name == "addition" else ECHO)


class TestADrainThatGaveUp:
    """A worker that outlived its drain is a failure, and the next smoke will not clear it.

    Both halves were missing. `watch` stops on settled logs, so the cap need not have fired and the digest was free to conclude *ready* while one of this rehearsal's own processes was still generating against the account the run is about to use. And nothing tends a smoke, so nothing would ever stop it — while the next rehearsal's `rmtree` took its logs and its in-flight record out from under it.
    """

    def test_a_worker_still_running_fails_the_rehearsal(self, tmp_path: Path) -> None:
        _, plan = planned(tmp_path)
        for task in plan.manifest.tasks:
            _land(Path(plan.log_dir), task.name)

        result = conclude(
            plan,
            logs=observe_logs(plan.log_dir),
            capped=False,
            elapsed=1.0,
            waived=(),
            lingering=["worker-1"],
        )

        assert result.outcome is Outcome.FAILED
        assert any("worker-1" in line for line in result.errors)

    def test_the_next_rehearsal_refuses_to_clear_the_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace, _ = planned(tmp_path)

        def alive(record: Path, workers_dir: Path) -> _Alive:
            return _Alive(["worker-7"])

        monkeypatch.setattr(run_module, "resolve_inflight", alive)

        with pytest.raises(LaunchError, match="worker-7"):
            prepare(workspace, synth_manifest([ADDITION]))

    def test_a_directory_nothing_is_using_is_cleared(self, tmp_path: Path) -> None:
        # the common path, and the one the refusal must not cost: a rehearsal
        # replaces the last one's logs rather than accumulating them
        workspace, plan = planned(tmp_path)
        _land(Path(plan.log_dir), "addition")

        again = prepare(workspace, synth_manifest([ADDITION]))

        assert not list(Path(again.log_dir).glob("*.eval"))


class TestWritingItDown:
    """A rehearsal nobody can read the result of has not passed.

    Both writes used to go to `steward.log` and change nothing. Lose the digest and keep the journal and the gate on the next launch says *rehearsed* while the terminal points at a file that is not there — so a write that fails is an error of the rehearsal, and the journal records the verdict that follows from it.
    """

    def recorded(self, workspace: Workspace, result: Smoke) -> Smoke:
        return run_module._record(workspace, result, notification=False)

    def test_a_digest_that_cannot_be_written_fails_the_rehearsal(
        self, tmp_path: Path
    ) -> None:
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        # a directory where the file goes: the write raises, nothing is lost
        (workspace.smoke / "digest.md").mkdir(parents=True)

        final = self.recorded(workspace, Smoke(log_dir=str(workspace.smoke)))

        assert final.outcome is Outcome.FAILED
        assert any("digest could not be written" in line for line in final.errors)

    def test_and_the_journal_records_that_rather_than_a_pass(
        self, tmp_path: Path
    ) -> None:
        # which is what keeps the gate honest: the newest smoke is the answer,
        # and this one did not pass
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        (workspace.smoke / "digest.md").mkdir(parents=True)

        self.recorded(
            workspace, Smoke(identifiers=("a",), log_dir=str(workspace.smoke))
        )

        assert read_smoked(read_journal(workspace.journal).events).identifiers == (
            frozenset()
        )

    def test_a_rehearsal_that_records_cleanly_is_unchanged(
        self, tmp_path: Path
    ) -> None:
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        workspace.smoke.mkdir(parents=True)
        result = Smoke(identifiers=("a",), log_dir=str(workspace.smoke))

        assert self.recorded(workspace, result) == result
        assert (workspace.smoke / "digest.md").exists()


class _Alive:
    """An in-flight read that reports these workers as running."""

    def __init__(self, workers: list[str]) -> None:
        self.running = [_Worker(one) for one in workers]


class _Worker:
    def __init__(self, worker: str) -> None:
        self.worker = worker


class TestAJournalThatWouldNotTakeIt:
    """The append is a write and then an `fsync`, so a failure does not mean the line is absent.

    An `fsync` that failed after the write landed leaves a journal whose newest smoke says `passed` — and `read_smoked` takes the newest whatever it concluded, so the next launch reads a pass nobody returned. Amending the value handed back settles nothing about the file. A second event does, in the safe direction for both shapes: where the first line landed this supersedes it, and where it did not this is the only record.
    """

    def flaky(
        self, monkeypatch: pytest.MonkeyPatch, seen: list[dict[str, Any]]
    ) -> None:
        """An append whose line lands and whose sync does not, once."""
        real = run_module.append_event

        def once(path: Path, type: str, **fields: Any) -> None:
            seen.append(fields)
            real(path, type, **fields)
            if len(seen) == 1:
                raise OSError("input/output error")

        monkeypatch.setattr(run_module, "append_event", once)

    def rehearsed(self, tmp_path: Path) -> Workspace:
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        workspace.smoke.mkdir(parents=True)
        return workspace

    def test_a_failed_append_is_followed_by_a_corrective_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = self.rehearsed(tmp_path)
        seen: list[dict[str, Any]] = []
        self.flaky(monkeypatch, seen)

        final = run_module._record(
            workspace,
            Smoke(identifiers=("a",), log_dir=str(workspace.smoke)),
            notification=False,
        )

        assert [one["verdict"] for one in seen] == ["passed", "failed"]
        assert final.outcome is Outcome.FAILED

    def test_and_the_journal_no_longer_reads_as_a_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the whole point: the fold takes the newest smoke, so the correction is
        # what a later launch reads
        workspace = self.rehearsed(tmp_path)
        self.flaky(monkeypatch, [])

        run_module._record(
            workspace,
            Smoke(identifiers=("a",), log_dir=str(workspace.smoke)),
            notification=False,
        )

        assert read_smoked(read_journal(workspace.journal).events).identifiers == (
            frozenset()
        )

    def test_a_journal_that_takes_neither_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = self.rehearsed(tmp_path)

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(run_module, "append_event", refuse)

        final = run_module._record(
            workspace, Smoke(log_dir=str(workspace.smoke)), notification=False
        )

        assert final.outcome is Outcome.FAILED
        assert "may read an older rehearsal as current" in workspace.log.read_text()
