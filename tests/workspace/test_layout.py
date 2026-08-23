"""Finding a workspace, and knowing when there is not one."""

from pathlib import Path

from inspect_steward._workspace import Workspace, create_workspace


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
