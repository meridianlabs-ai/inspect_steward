"""Propagating a workspace, against two directories on this machine.

`filesystem()` treats a local path as a filesystem like any other, so a second
`tmp_path` exercises the same code an S3 prefix does — the same `put_file`, the
same `mkdir`, the same failure handling. What a bucket would add is latency and
credentials, neither of which is what these cases are about.

The one thing that cannot be tested here is the destination being genuinely
slow, so the budget is asserted by handing it a budget of zero: what matters is
that a propagation which runs out says so and leaves the rest for the next turn,
not how many bytes per second it managed.
"""

from pathlib import Path

import pytest
from inspect_steward._workspace import (
    Workspace,
    steward_log,
    sync_target,
    sync_workspace,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    """A workspace with one of everything the deny list has an opinion about."""
    root = tmp_path / "sweep"
    root.mkdir()
    (root / "journal.jsonl").write_text('{"type":"initialized"}\n', encoding="utf-8")
    (root / "status.md").write_text("# status\n", encoding="utf-8")
    (root / "_steward.yaml").write_text("max_workers: 2\n", encoding="utf-8")
    (root / "evalset.py").write_text("eval_set()\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("bootstrap\n", encoding="utf-8")
    (root / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-secret\n", encoding="utf-8")
    (root / ".gitignore").write_text(".steward/\n", encoding="utf-8")
    (root / "logs").mkdir()
    return Workspace.at(root)


def target(tmp_path: Path) -> str:
    return str(tmp_path / "elsewhere")


def landed(destination: str) -> set[str]:
    return {path.name for path in Path(destination).iterdir()}


# --- where it goes ------------------------------------------------------


@pytest.mark.parametrize(
    ("sync", "expected"),
    [
        pytest.param(None, "s3://acme/oct/logs", id="no_preference"),
        pytest.param("auto", "s3://acme/oct/logs", id="auto"),
        pytest.param(False, None, id="declined"),
        pytest.param("s3://somewhere/else", "s3://somewhere/else", id="named"),
    ],
)
def test_where_a_workspace_propagates_to(
    sync: str | bool | None, expected: str
) -> None:
    # the log directory by default, because that is where the files being
    # explained already are -- and remoteness is not consulted at all, since a
    # mounted NAS has the same need and the old rule silently skipped it
    assert sync_target(sync, "s3://acme/oct/logs") == expected


# --- what leaves --------------------------------------------------------


def test_everything_at_the_top_level_leaves_except_the_deny_list(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Exclusionary, because the point is carrying out what nobody predicted.

    An allow-list leaves an agent's `analysis.md` behind by construction, which
    is the failure you notice last. The two things it must not carry are the
    ones excluded by kind rather than by name.
    """
    (workspace.root / "analysis.md").write_text("what I found\n", encoding="utf-8")
    destination = target(tmp_path)

    report = sync_workspace(workspace, destination)

    assert landed(destination) == {
        "journal.jsonl",
        "status.md",
        "_steward.yaml",
        "evalset.py",
        "analysis.md",
    }
    assert set(report.carried) == landed(destination)
    # the dotfile rule earns its place here rather than in a comment
    assert ".env" not in landed(destination)
    assert "AGENTS.md" not in landed(destination)
    assert "logs" not in landed(destination)


def test_the_machine_logs_leave_by_name_and_land_flat(
    workspace: Workspace, tmp_path: Path
) -> None:
    # they live under `.steward/` because that is the category they belong to,
    # and a remote reader still needs the answer to *is the machinery working*
    steward_log(workspace.log, "could not archive a log")
    workspace.timer_log.write_text("cron said something\n", encoding="utf-8")
    destination = target(tmp_path)

    sync_workspace(workspace, destination)

    assert {"steward.log", "timer.log"} <= landed(destination)
    assert "could not archive a log" in (
        Path(destination, "steward.log").read_text(encoding="utf-8")
    )


def test_a_file_that_would_read_as_an_eval_log_is_left_where_it_is(
    workspace: Workspace, tmp_path: Path
) -> None:
    """The hazard the destination changed into existence.

    The files land *in* the log directory, so one that looks like a log becomes
    one — listed by `observe_logs`, read as a header, and reported as damage
    every turn. Narrow (an `.eval`, or an ISO-timestamp prefix and a `.json`
    suffix) and not narrow enough to leave to luck, because the policy above is
    exclusionary and one unanticipated file will eventually be named like that.
    """
    (workspace.root / "2026-10-02T14-00-00_notes.json").write_text(
        "{}", encoding="utf-8"
    )
    (workspace.root / "leftovers.eval").write_text("not really", encoding="utf-8")
    destination = target(tmp_path)

    report = sync_workspace(workspace, destination)

    assert set(report.refused) == {"2026-10-02T14-00-00_notes.json", "leftovers.eval"}
    assert not {"2026-10-02T14-00-00_notes.json", "leftovers.eval"} & landed(
        destination
    )
    # and it is said out loud, since a file silently left behind is the failure
    # the exclusionary policy exists to avoid
    assert "2026-10-02T14-00-00_notes.json" in workspace.log.read_text(encoding="utf-8")


def test_a_symlink_is_not_followed(workspace: Workspace, tmp_path: Path) -> None:
    """The route around the deny list, and it carries the one file it protects.

    `public-config -> .env` is not a dotfile, is a perfectly good file, and
    passes every check by name — and `put_file` dereferences it, so the
    credential lands in the bucket by exactly the path the rule exists to
    close. The deny list is a rule about names and a symlink is a name meaning
    a different name, so they are not followed at all.
    """
    (workspace.root / "public-config").symlink_to(workspace.root / ".env")
    destination = target(tmp_path)

    report = sync_workspace(workspace, destination)

    assert report.unfollowed == ["public-config"]
    assert "public-config" not in landed(destination)
    assert not any(
        "sk-ant-secret" in Path(destination, name).read_text(encoding="utf-8")
        for name in landed(destination)
    )
    # named rather than dropped, because silently leaving something behind is
    # the failure the exclusionary policy exists to avoid
    assert "public-config" in workspace.log.read_text(encoding="utf-8")


# --- what it does not do twice ------------------------------------------


def test_a_file_the_destination_already_has_is_not_sent_again(
    workspace: Workspace, tmp_path: Path
) -> None:
    """`journal.jsonl` grows, and a multi-day run tends every ten minutes.

    Re-uploading it every turn is cheap to a bucket in the same region and not
    cheap at all on the one pipe an air-gapped runner has. Size and mtime, both
    local, so nothing here compares this machine's clock against a store's.
    """
    destination = target(tmp_path)
    first = sync_workspace(workspace, destination)

    (workspace.root / "status.md").write_text("# status\n\nlater\n", encoding="utf-8")
    second = sync_workspace(workspace, destination)

    assert set(first.carried) == landed(destination)
    assert second.carried == ["status.md"]
    assert "journal.jsonl" in second.skipped


def test_a_lost_record_costs_one_propagation_and_nothing_else(
    workspace: Workspace, tmp_path: Path
) -> None:
    # `.steward/` is documented as safe to delete, so the record has to be
    # disposable in fact and not only in intent
    destination = target(tmp_path)
    sync_workspace(workspace, destination)
    workspace.synced.unlink()

    again = sync_workspace(workspace, destination)

    assert "journal.jsonl" in again.carried
    assert not again.failures


def test_a_new_destination_starts_from_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    """The record answers *does the far end have this*, which is about one far end.

    Kept by filename alone, pointing `--sync` somewhere new would report every
    file as already sent and leave the new destination empty — the skip turning
    from an optimisation into a silent failure to propagate at all.
    """
    first = target(tmp_path)
    sync_workspace(workspace, first)
    second = str(tmp_path / "watched")

    report = sync_workspace(workspace, second)

    assert landed(second) == landed(first)
    assert report.skipped == []
    # and going back is a full propagation too, since the record holds one
    # destination -- one re-send is the right price for an unknown answer
    assert sync_workspace(workspace, first).skipped == []


# --- when it cannot finish, or cannot start -----------------------------


def test_a_spent_budget_stops_and_says_what_is_left(
    workspace: Workspace, tmp_path: Path
) -> None:
    """A slow pipe becomes a reported fact rather than a long tend.

    And the next turn carries what this one could not, rather than starting
    over — which is the whole reason skipping unchanged files and bounding the
    time are one design rather than two.
    """
    destination = target(tmp_path)

    stopped = sync_workspace(workspace, destination, budget=-1.0)

    assert stopped.carried == []
    assert len(stopped.remaining) == 4
    assert "ran out of time" in workspace.log.read_text(encoding="utf-8")

    resumed = sync_workspace(workspace, destination)
    # everything that did not fit, plus `steward.log` -- which the first
    # attempt created by recording that it ran out, and which is exactly the
    # file a remote reader would want to find after a turn like that
    assert set(resumed.carried) == {*stopped.remaining, "steward.log"}


def test_an_unwritable_destination_is_recorded_rather_than_raised(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Step 22's *done when*, and workflow.md §9.3's whole posture.

    An eval must not fail because a bucket was briefly unreachable. The turn
    that called this has already happened and already been recorded; what is
    lost is ten minutes of a remote reader's freshness.
    """
    wall = tmp_path / "wall"
    wall.write_text("not a directory", encoding="utf-8")

    report = sync_workspace(workspace, str(wall / "under"))

    assert report.carried == []
    assert report.failures and "could not reach" in report.failures[0]
    assert "could not reach" in workspace.log.read_text(encoding="utf-8")


def test_a_workspace_with_nothing_to_carry_does_nothing(tmp_path: Path) -> None:
    empty = Workspace.at(tmp_path / "bare")

    report = sync_workspace(empty, target(tmp_path))

    assert report.carried == []
    assert not Path(target(tmp_path)).exists()
