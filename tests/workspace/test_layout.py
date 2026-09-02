"""Finding a workspace, knowing when there is not one, and where its logs go."""

from pathlib import Path

import pytest
from inspect_steward._workspace import Workspace, create_workspace, resolve_log_dir

from .._logs import synth_manifest


def test_finds_the_workspace_from_within(tmp_path: Path) -> None:
    create_workspace(tmp_path / "sweep", git=False)
    deep = tmp_path / "sweep" / "logs" / "nested"
    deep.mkdir(parents=True)

    found = Workspace.find(deep)

    assert found is not None
    assert found.root == (tmp_path / "sweep").resolve()


def test_finds_nothing_outside_one(tmp_path: Path) -> None:
    # the journal is the marker, so a directory that merely looks workspace-ish
    # is not one
    (tmp_path / "AGENTS.md").write_text("# not a workspace\n")
    (tmp_path / "evalset.py").write_text("")

    assert Workspace.find(tmp_path) is None


def test_finds_the_innermost_workspace(tmp_path: Path) -> None:
    # a sweep nested inside another project's workspace belongs to itself
    create_workspace(tmp_path / "outer", git=False)
    create_workspace(tmp_path / "outer" / "inner", git=False)

    found = Workspace.find(tmp_path / "outer" / "inner")

    assert found is not None
    assert found.root.name == "inner"


def test_find_definition_prefers_nothing_when_there_is_none(tmp_path: Path) -> None:
    assert Workspace.at(tmp_path).find_definition() is None


@pytest.mark.parametrize(
    ("configured", "root", "expected"),
    [
        # nothing said either way: the workspace's own directory
        (None, None, "<ws>/logs"),
        ("", None, "<ws>/logs"),
        # a root answers only the silence
        (None, "<tmp>/runs", "<tmp>/runs/sweep"),
        (None, "s3://bucket/runs", "s3://bucket/runs/sweep"),
        (None, "s3://bucket/runs/", "s3://bucket/runs/sweep"),
        # a definition that names one is the source of truth, root or no root:
        # the root supplies a default, it never rebases a stated answer
        ("results", None, "<ws>/results"),
        ("results", "<tmp>/runs", "<ws>/results"),
        ("/var/evals", "<tmp>/runs", "/var/evals"),
        ("s3://elsewhere/logs", "<tmp>/runs", "s3://elsewhere/logs"),
    ],
)
def test_resolve_log_dir(
    configured: str | None, root: str | None, expected: str, tmp_path: Path
) -> None:
    workspace = Workspace.at(tmp_path / "sweep")

    def expand(value: str) -> str:
        return value.replace("<ws>", str(workspace.root)).replace(
            "<tmp>", str(tmp_path.resolve())
        )

    resolved = resolve_log_dir(
        workspace,
        synth_manifest([], log_dir=configured),
        expand(root) if root is not None else None,
    )

    assert resolved == expand(expected)


def test_a_root_gives_each_workspace_its_own_directory(tmp_path: Path) -> None:
    # two workspaces under one root must not share a log directory, since each
    # propagates its own status.md and journal.jsonl into it
    root = str(tmp_path / "runs")
    empty = synth_manifest([])

    first = resolve_log_dir(Workspace.at(tmp_path / "swe"), empty, root)
    second = resolve_log_dir(Workspace.at(tmp_path / "math"), empty, root)

    assert first != second


def _flow_spec(path: Path) -> Path:
    """A Python file that reads as a flow spec."""
    path.write_text("from inspect_flow import FlowSpec\n\nspec = FlowSpec(tasks=[])\n")
    return path


def test_a_flow_spec_is_found_under_whatever_name_its_author_gave_it(
    tmp_path: Path,
) -> None:
    """A flow spec is a file its author names, so discovery reads rather than guesses."""
    spec = _flow_spec(tmp_path / "swebench.py")

    assert Workspace.at(tmp_path).find_definition() == spec


def test_flows_auto_include_file_is_never_the_definition(tmp_path: Path) -> None:
    """`_flow.py` imports inspect_flow and is merged into whatever spec runs.

    Classifying it as a definition would make every workspace that keeps shared
    defaults beside its spec ambiguous.
    """
    _flow_spec(tmp_path / "_flow.py")
    spec = _flow_spec(tmp_path / "config.py")

    assert Workspace.at(tmp_path).find_definition() == spec


def test_python_that_is_not_a_definition_is_passed_over(tmp_path: Path) -> None:
    (tmp_path / "helpers.py").write_text("def score(x: int) -> int:\n    return x\n")
    spec = _flow_spec(tmp_path / "swebench.py")

    assert Workspace.at(tmp_path).find_definition() == spec


def test_two_files_that_each_read_as_a_definition_choose_neither(
    tmp_path: Path,
) -> None:
    """Nothing here can tell which one the operator meant, so the caller says so."""
    _flow_spec(tmp_path / "swebench.py")
    _flow_spec(tmp_path / "gpqa.py")

    workspace = Workspace.at(tmp_path)

    assert workspace.find_definition() is None
    assert [path.name for path in workspace.definition_candidates()] == [
        "gpqa.py",
        "swebench.py",
    ]


def test_a_conventional_name_settles_it_without_reading_anything(
    tmp_path: Path,
) -> None:
    """The placeholder `init` writes is empty, and an empty file classifies as nothing."""
    (tmp_path / "config.py").write_text("")
    _flow_spec(tmp_path / "swebench.py")

    found = Workspace.at(tmp_path).find_definition()

    assert found is not None
    assert found.name == "config.py"


def test_a_workspace_scaffolded_before_python_specs_still_resolves(
    tmp_path: Path,
) -> None:
    (tmp_path / "flow.yaml").write_text("tasks: []\n")

    found = Workspace.at(tmp_path).find_definition()

    assert found is not None
    assert found.name == "flow.yaml"
