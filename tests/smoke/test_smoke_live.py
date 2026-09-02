"""A rehearsal, for real: two workers, a real scan, and the digest it leaves.

**Budget: one smoke** (plan.md §10) — one capture and two workers, in one test, asserting everything a live run is the only way to establish. Everything else about the smoke is layer 1 beside this file.

What only a real run can show is the *isolation*, and it is the claim worth spending a launch on: a rehearsal writes its logs, its selection documents and its in-flight record where nothing belonging to the run will ever look, and commits no desired state at all. Each of those was a way to make the launch that follows worse, and one of them — the in-flight record — actually did, leaving both tasks stalled before the real launch had run a sample.
"""

from pathlib import Path

from inspect_steward._smoke import SCAN_COVERAGE, Outcome, Verdict
from inspect_steward._smoke.run import smoke
from inspect_steward._workspace import (
    SMOKED,
    Held,
    Workspace,
    create_workspace,
    read_journal,
    read_smoked,
)

FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"

SIMPLE = FIXTURES / "simple_evalset.py"
"""`addition` (2 samples) and `echo` (1 sample × 2 epochs) on `mockllm/model` — four sample records in all, which `--samples 1` truncates to three."""


def test_a_rehearsal_runs_truncated_and_leaves_the_run_untouched(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)

    result = smoke(workspace, SIMPLE, samples=1, cap=3)

    assert not isinstance(result, Held)
    assert result.outcome is Outcome.PASSED, result.errors

    # **truncated**: `addition` gives one sample instead of two, `echo` still
    # gives two because a slice cuts the dataset and not the epochs. The
    # manifest keeps the run's own four, untouched by the slice
    assert result.landed == 3
    assert result.population == 4

    # **the logs are somewhere nothing will mistake them for results**, and
    # `logs/` was never even created
    assert not workspace.logs.exists()
    assert sorted(path.suffix for path in workspace.smoke.glob("*.eval")) == [
        ".eval",
        ".eval",
    ]

    # **no desired state.** A rehearsal is a question about a definition, and
    # answering it must not change what the workspace converges toward
    assert not workspace.manifest.exists()

    # **the run's own machine state is untouched** -- the regression this file
    # exists for. Sharing the in-flight record spends the run's attempt budget
    # on rehearsals, and `reconcile` stops respawning after two
    assert not workspace.inflight.exists()
    assert not workspace.workers.exists()
    assert workspace.smoke_inflight.exists()

    # **the scan ran, folded and finalized**, which no other non-signoff path
    # does -- a rehearsal is terminal the moment it ends
    scans = list(workspace.smoke.glob("scans/scan_id=*/*.parquet"))
    assert [path.name for path in scans] == ["scoring_integrity.parquet"]
    assert (scans[0].parent / "_summary.json").exists()

    # **and it reached every transcript**, which is the only place that check is
    # calibrated against a real scan rather than against a fixture: a rule that
    # over-read a legitimate configuration as a gap would fail exactly here
    coverage = next(one for one in result.probe.checks if one.name == SCAN_COVERAGE)
    assert coverage.verdict is Verdict.PASSED, coverage.detail

    # **the artifact an agent is told to trust**, and the journal record a
    # later launch consults
    assert (workspace.smoke / "digest.md").exists()
    events = read_journal(workspace.journal).events
    assert [event.type for event in events if event.type == SMOKED] == [SMOKED]
    rehearsal = read_smoked(events)
    assert rehearsal.identifiers == set(result.identifiers)
    # the shape reaches the journal too, which is the half of the coverage
    # question a set of identifiers cannot answer
    assert rehearsal.digest == result.digest != ""
