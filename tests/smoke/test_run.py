"""How a rehearsal is set up, and the two ways it must not touch the run it precedes.

The truncation rides the *workers*: the manifest a smoke captures is the manifest the launch will capture, which is what makes the coverage question a set comparison and what makes the two digests comparable at all. And the rehearsal's machine state is its own — a boundary that reads like tidiness and is not, because `reconcile` stops respawning a task after two spent attempts and a rehearsal writing into the run's record spends them.
"""

from pathlib import Path

from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from inspect_steward._evalset.manifest import (
    DEFAULT_RETRY_ON_ERROR,
    Manifest,
    ManifestScan,
)
from inspect_steward._evalset.observe import observe_logs
from inspect_steward._scan import scan_dir_location
from inspect_steward._smoke import Outcome, Probe, Verdict
from inspect_steward._smoke.checks import Check
from inspect_steward._smoke.digest import Smoke, digest_markdown, outcome
from inspect_steward._smoke.run import (
    DEFAULT_SAMPLES,
    Plan,
    fold,
    models,
    overrides,
    prepare,
    worker_overrides,
)
from inspect_steward._workspace import Workspace, create_workspace

from .._logs import SynthTask, synth_manifest

ADDITION = SynthTask("addition", samples=200)
ECHO = SynthTask("echo", samples=50)


def workspace(tmp_path: Path) -> Workspace:
    create_workspace(tmp_path, git=False)
    return Workspace.at(tmp_path)


def planned(
    tmp_path: Path,
    manifest: Manifest | None = None,
    *,
    samples: int = DEFAULT_SAMPLES,
) -> Plan:
    return prepare(
        workspace(tmp_path),
        manifest or synth_manifest([ADDITION, ECHO]),
        samples=samples,
    )


class TestTheTruncationRidesTheWorkers:
    """The capture stays whole; only the workers are told to run less."""

    def test_the_workers_are_told_the_slice_the_log_dir_and_every_raw_call(
        self, tmp_path: Path
    ) -> None:
        plan = planned(tmp_path, samples=3)

        told = overrides(plan, None)

        # a *slice*, not `max_samples` -- which is concurrency, and bounds how
        # many run at once rather than how many run at all
        assert told.limit == 3
        assert told.log_dir == str(Workspace.at(tmp_path).smoke)
        # every provider call, not the first five per model, which is what makes
        # the reasoning check answerable past the fifth turn
        assert told.log_model_api is True

    def test_the_manifest_keeps_the_runs_own_sample_counts(
        self, tmp_path: Path
    ) -> None:
        # **the reason the slice is not applied at capture.** A manifest
        # describing the rehearsal hashes a different digest from the launch's,
        # for the one reason that does not matter
        plan = planned(tmp_path, samples=1)

        assert [task.samples for task in plan.manifest.tasks] == [200, 50]

    def test_the_runs_own_overrides_survive_underneath(self, tmp_path: Path) -> None:
        from inspect_ai._eval.eval_set_overrides import EvalSetOverrides

        plan = planned(tmp_path, samples=2)

        told = overrides(plan, EvalSetOverrides(max_tasks=4))

        assert told.max_tasks == 4
        assert told.limit == 2

    def test_the_default_is_bounded_rather_than_magic(self) -> None:
        assert DEFAULT_SAMPLES == 2


class TestTheRehearsalKeepsToItself:
    """Where a rehearsal writes, and why it is not where the run writes."""

    def test_the_logs_and_the_machine_state_are_all_under_one_directory(
        self, tmp_path: Path
    ) -> None:
        space = workspace(tmp_path)
        plan = planned(tmp_path)

        assert Path(plan.log_dir) == space.smoke
        assert space.smoke_workers.parent == space.smoke
        assert space.smoke_inflight.parent == space.smoke

    def test_the_runs_in_flight_record_is_a_different_file(
        self, tmp_path: Path
    ) -> None:
        # measured rather than anticipated: `resolve_inflight` counts spent
        # attempts per identifier and `reconcile` stops respawning after two, so
        # two smokes sharing the record left every task stalled before the real
        # launch had run a sample
        space = workspace(tmp_path)

        assert space.smoke_inflight != space.inflight
        assert space.smoke_workers != space.workers

    def test_each_rehearsal_clears_the_last(self, tmp_path: Path) -> None:
        space = workspace(tmp_path)
        planned(tmp_path)
        stale = space.smoke / "2020-01-01_stale.eval"
        stale.write_text("old", encoding="utf-8")

        planned(tmp_path)

        assert not stale.exists()

    def test_a_rehearsal_writes_nothing_into_the_runs_log_directory(
        self, tmp_path: Path
    ) -> None:
        space = workspace(tmp_path)

        planned(tmp_path)

        assert not space.logs.exists()
        assert not space.manifest.exists()


class TestWhichModelsAreChecked:
    def test_every_model_the_manifest_names(self, tmp_path: Path) -> None:
        assert models(synth_manifest([ADDITION, ECHO])) == ["mockllm/model"]

    def test_the_scan_model_too(self, tmp_path: Path) -> None:
        # a scanner reviewing transcripts through a mis-resolved window is the
        # same failure as the eval running through one, and the half nobody
        # would think to check by hand
        named = models(synth_manifest([ADDITION]), scan_model="openai/gpt-5")

        assert named == ["mockllm/model", "openai/gpt-5"]

    def test_the_models_a_task_wires_up_by_role(self, tmp_path: Path) -> None:
        # a grader, a critic or an attacker generates against a context window
        # like anything else, and a definition where the interesting model *is*
        # a role would have been checked only on its main one
        manifest = synth_manifest([ADDITION])
        roles = manifest.model_copy(
            update={
                "tasks": [
                    manifest.tasks[0].model_copy(
                        update={"model_roles": {"grader": "openai/gpt-5"}}
                    )
                ]
            }
        )

        assert models(roles) == ["mockllm/model", "openai/gpt-5"]


class TestTheVerdict:
    """What makes a rehearsal pass, and why a cap is its own answer."""

    def failing(self) -> Probe:
        return Probe(checks=(Check("context_window", Verdict.FAILED, "no window"),))

    def test_a_clean_rehearsal_passes(self) -> None:
        assert outcome(Probe(), waived=(), capped=False, errors=0) is Outcome.PASSED

    def test_a_failed_check_fails_it(self) -> None:
        assert (
            outcome(self.failing(), waived=(), capped=False, errors=0) is Outcome.FAILED
        )

    def test_a_waived_check_does_not(self) -> None:
        # recorded rather than silent: the journal carries what was waived, so
        # a pass is honest about what it did not establish
        assert (
            outcome(self.failing(), waived=("context_window",), capped=False, errors=0)
            is Outcome.PASSED
        )

    def test_only_a_check_that_would_have_blocked_counts_as_waived(self) -> None:
        # waiving a check that then passed established nothing and hid nothing,
        # so a verdict claiming a caveat over it would claim one the rehearsal
        # does not have
        passing = Smoke(
            probe=Probe(checks=(Check("reasoning", Verdict.UNEXERCISED, "none"),)),
            waived=("reasoning",),
        )
        blocked = Smoke(probe=self.failing(), waived=("context_window",))

        assert passing.waived_away == ()
        assert blocked.waived_away == ("context_window",)
        assert "waived" not in digest_markdown(passing).splitlines()[2]
        assert "context_window waived" in digest_markdown(blocked).splitlines()[2]

    def test_a_rehearsal_that_could_not_run_fails_without_any_check_failing(
        self,
    ) -> None:
        assert outcome(Probe(), waived=(), capped=False, errors=1) is Outcome.FAILED

    def test_a_truncated_rehearsal_is_ready(self) -> None:
        """The cap firing mid-sample is the tool working, not a defect to fix.

        A smoke runs a couple of samples under a deadline precisely so it can
        be stopped. Reading the stop as a verdict discarded every check that
        had already answered and refused the launch it had just cleared.
        """
        assert (
            outcome(Probe(), waived=(), capped=True, errors=0, landed=3)
            is Outcome.PASSED
        )

    def test_a_cap_that_established_nothing_is_its_own_outcome(self) -> None:
        # the narrow case left: no sample landed, so no check could answer and
        # there is nothing to have an opinion about. A different thing to look
        # into than a check that came back wrong
        assert (
            outcome(Probe(), waived=(), capped=True, errors=0, landed=0)
            is Outcome.CAPPED
        )

    def test_a_cap_excuses_nothing_a_rehearsal_actually_found(self) -> None:
        # the deadline explains a short slice and nothing else -- a check that
        # came back wrong under a cap came back wrong
        assert (
            outcome(self.failing(), waived=(), capped=True, errors=0, landed=2)
            is Outcome.FAILED
        )
        assert (
            outcome(Probe(), waived=(), capped=True, errors=0, errored=1, landed=2)
            is Outcome.FAILED
        )


class TestWhereTheRehearsalsScanRowsGo:
    """A redirected scan makes `log_dir` irrelevant, and the rehearsal was addressing the run's own directory.

    `scan_dir_location` is `{scans or log_dir}/scan_id={scan_id}`, so a definition that redirects its scans somewhere shared takes the log directory out of the answer entirely. Under the run's own eval set id the rehearsal then resolved to the run's own scan directory: rehearsal rows written into it, its summary rewritten, and — since the finalize prunes rows naming logs outside the directory it was handed — the run's rows pruned using the rehearsal's log directory.

    Redirecting the rehearsal's scans locally does not fix it: a worker reads `ScannerConfig.scans` out of the definition it executes, so it would keep writing to the redirect while Steward initialized somewhere else, and `verify_selection_scan_dir` would refuse at startup. A distinct scan id does fix it, and every path takes it from the plan.
    """

    def test_the_rehearsal_records_under_its_own_id(self, tmp_path: Path) -> None:
        create_workspace(tmp_path, git=False)
        manifest = synth_manifest([ADDITION]).model_copy(
            update={"eval_set_id": "the-run"}
        )

        plan = prepare(Workspace.at(tmp_path), manifest, cap=0)

        assert plan.scan_id != "the-run"
        assert plan.scan_dir is not None
        assert f"scan_id={plan.scan_id}" in plan.scan_dir

    def test_a_redirected_scan_does_not_resolve_to_the_runs_directory(
        self, tmp_path: Path
    ) -> None:
        # the destructive case, stated as the two locations it turns on: the
        # redirect makes both ignore `log_dir`, so only the id separates them
        redirect = str(tmp_path / "shared-scans")
        create_workspace(tmp_path, git=False)
        manifest = synth_manifest([ADDITION]).model_copy(
            update={
                "eval_set_id": "the-run",
                "scan": ManifestScan(scans=redirect),
            }
        )

        plan = prepare(Workspace.at(tmp_path), manifest, cap=0)

        theirs = scan_dir_location(
            log_dir=str(Workspace.at(tmp_path).logs), scan_id="the-run", scans=redirect
        )
        ours = scan_dir_location(
            log_dir=plan.log_dir, scan_id=plan.scan_id, scans=redirect
        )
        assert ours != theirs

    def test_every_path_uses_the_same_id(self, tmp_path: Path) -> None:
        # the fold and the fleet re-resolved it independently, and resolving
        # against the manifest's id after minting a different one raises
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        manifest = synth_manifest([ADDITION]).model_copy(
            update={"eval_set_id": "the-run"}
        )

        plan = prepare(workspace, manifest, cap=0)
        fold(plan, observe_logs(plan.log_dir))

        assert (Path(plan.log_dir) / ".eval-set-id").read_text().strip() == plan.scan_id


class TestTheRetryBudgetTheWorkersGet:
    """A rehearsal at no retries is a rehearsal of something the launch will not do.

    The real fleet goes through `worker_overrides`, which supplies `DEFAULT_RETRY_ON_ERROR` where neither the definition nor the run named a number. The rehearsal read the raw manifest overrides, so it ran at inspect's own default of none — a transient blip errored a sample the launch would have retried, which now fails the rehearsal, and the retry path itself was never exercised.
    """

    def told(self, tmp_path: Path, manifest: Manifest) -> EvalSetOverrides:
        """What a rehearsal worker is finally handed, through the fleet's own route."""
        return overrides(planned(tmp_path, manifest), worker_overrides(manifest))

    def test_the_rehearsal_gets_the_runs_retry_budget(self, tmp_path: Path) -> None:
        assert (
            self.told(tmp_path, synth_manifest([ADDITION])).retry_on_error
            == DEFAULT_RETRY_ON_ERROR
        )

    def test_and_a_definition_that_named_one_keeps_it(self, tmp_path: Path) -> None:
        # a default and never a constraint: how many attempts a sample deserves
        # is the eval author's call, and `retry_on_error: 0` still means zero
        manifest = synth_manifest([ADDITION]).model_copy(
            update={"overrides": EvalSetOverrides(retry_on_error=0)}
        )

        assert self.told(tmp_path, manifest).retry_on_error == 0
