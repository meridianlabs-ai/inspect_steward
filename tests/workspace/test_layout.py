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
