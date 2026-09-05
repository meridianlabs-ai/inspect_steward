"""Creating a workspace, and — the part with teeth — re-running over one.

`init` runs more than once in a workspace's life: someone re-runs it after an
upgrade, or on a directory they built by hand, or inside a repository that
already exists. Every authored file in there is someone's work, so the property
under test throughout is that it only ever adds.
"""

import json
from pathlib import Path

import pytest
from inspect_steward._evalset.detect import DefinitionType
from inspect_steward._workspace import (
    GITIGNORE_ENTRIES,
    CreateReport,
    DirectivesError,
    Outcome,
    Workspace,
    create_workspace,
)


def outcomes(report: CreateReport) -> dict[str, Outcome]:
    """Outcome per path, for asserting against a whole run at once."""
    return {step.path: step.outcome for step in report.steps}


def test_creates_the_workspace(tmp_path: Path) -> None:
    report = create_workspace(tmp_path / "sweep", git=False)
    workspace = report.workspace

    assert outcomes(report) == {
        "AGENTS.md": Outcome.CREATED,
        "CLAUDE.md": Outcome.CREATED,
        "_steward.yaml": Outcome.CREATED,
        "evalset.py": Outcome.CREATED,
        ".gitignore": Outcome.CREATED,
        "git": Outcome.SKIPPED,
        "journal.jsonl": Outcome.CREATED,
    }
    assert workspace.agents.read_text().startswith("# AGENTS.md")
    assert workspace.directives.exists()
    # the definition is a placeholder, not a guess at what is being measured:
    # comments only, carrying the one thing an operator cannot infer
    placeholder = workspace.definition("evalset").read_text()
    assert all(
        not line.strip() or line.startswith("#") for line in placeholder.splitlines()
    )
    assert "ending in a call to eval_set()" in placeholder

    # created on demand by the steps that own them, not here -- and git would
    # not carry an empty directory anyway
    assert not workspace.logs.exists()
    assert not workspace.logs_archive.exists()
    assert not workspace.state.exists()
    assert not workspace.status.exists()


def test_opens_the_journal_with_a_real_event(tmp_path: Path) -> None:
    # the journal is what makes the directory a workspace, so it starts with a
    # record rather than as an empty file
    workspace = create_workspace(tmp_path, git=False).workspace
    events = [json.loads(line) for line in workspace.journal.read_text().splitlines()]

    assert len(events) == 1
    assert events[0]["type"] == "initialized"
    assert events[0]["definition"] == "evalset.py"
    assert events[0]["ts"].endswith("Z")


@pytest.mark.parametrize(
    ("type", "filename", "empty"),
    [
        # only the evalset placeholder has anything to say: hawk has no
        # log-directory option at all, and a flow spec's is a different key
        ("evalset", "evalset.py", False),
        ("flow", "config.py", True),
        ("hawk", "hawk.yaml", True),
    ],
)
def test_definition_type_chooses_the_filename(
    type: DefinitionType, filename: str, empty: bool, tmp_path: Path
) -> None:
    create_workspace(tmp_path, type=type, git=False)
    assert ((tmp_path / filename).read_text() == "") is empty


def test_rerunning_changes_nothing(tmp_path: Path) -> None:
    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    workspace.directives.write_text("never spend over $200 without asking\n")

    report = create_workspace(tmp_path, git=False)

    assert set(outcomes(report).values()) == {Outcome.KEPT, Outcome.SKIPPED}
    assert not report.created_anything
    # the operator's own work, and the record, both survive untouched
    assert workspace.directives.read_text() == "never spend over $200 without asking\n"
    assert len(workspace.journal.read_text().splitlines()) == 1


def test_keeps_a_definition_that_is_already_there(tmp_path: Path) -> None:
    # the contract is "any program culminating in one eval_set() call", so a
    # workspace wrapped around an existing definition must not gain a second one
    (tmp_path / "flow.yaml").write_text("tasks: []\n")

    report = create_workspace(tmp_path, type="evalset", git=False)

    assert outcomes(report)["flow.yaml"] is Outcome.KEPT
    assert not (tmp_path / "evalset.py").exists()
    assert (tmp_path / "flow.yaml").read_text() == "tasks: []\n"


def test_gitignore_gains_only_what_is_missing(tmp_path: Path) -> None:
    # a workspace made inside an existing project may already have one, and the
    # rules in it are not Steward's to rewrite
    (tmp_path / ".gitignore").write_text("*.pyc\nlogs/\n")

    report = create_workspace(tmp_path, git=False)

    assert outcomes(report)[".gitignore"] is Outcome.UPDATED
    contents = (tmp_path / ".gitignore").read_text()
    assert contents.startswith("*.pyc\nlogs/\n")
    assert contents.count("logs/\n") == 1
    assert all(entry in contents for entry in GITIGNORE_ENTRIES)


def test_claude_points_at_agents(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path, git=False).workspace
    # a symlink where the platform allows one, an import where it does not --
    # either way a pointer rather than a copy that can drift
    assert workspace.claude.read_text() in (
        workspace.agents.read_text(),
        "@AGENTS.md\n",
    )


def test_an_unconverted_workspace_is_refused_rather_than_completed(
    tmp_path: Path,
) -> None:
    """The one case where `init` adding a missing file destroys something.

    `init` completing a partial workspace is the whole point of it, and
    `_steward.yaml` missing is exactly the state an unconverted workspace is
    in. Writing the template there buries the author's standing rules under a
    file that parses as *no rules* — and takes the refusal in `read_directives`
    with it, since that only fires while the new file is absent. So this is the
    one file `init` refuses over instead of supplying.
    """
    (tmp_path / "_steward.md").write_text(
        "---\nmax_workers: 8\n---\n", encoding="utf-8"
    )

    with pytest.raises(DirectivesError, match="_steward.md"):
        create_workspace(tmp_path, git=False)

    # and nothing was written on the way to refusing, so the directory is still
    # the one the author had rather than half a workspace
    assert not (tmp_path / "_steward.yaml").exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_a_converted_workspace_is_completed_as_usual(tmp_path: Path) -> None:
    # a leftover _steward.md beside a real _steward.yaml is somebody's backup,
    # and init has nothing to decide about it
    (tmp_path / "_steward.md").write_text(
        "---\nmax_workers: 8\n---\n", encoding="utf-8"
    )
    (tmp_path / "_steward.yaml").write_text("max_workers: 2\n", encoding="utf-8")

    report = create_workspace(tmp_path, git=False)

    assert outcomes(report)["_steward.yaml"] == Outcome.KEPT
    assert (tmp_path / "_steward.yaml").read_text(
        encoding="utf-8"
    ) == "max_workers: 2\n"
