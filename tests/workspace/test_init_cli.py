"""`steward init` and `steward runbook` as a user meets them."""

from pathlib import Path

from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._workspace import Workspace


def test_init_creates_a_named_directory(tmp_path: Path) -> None:
    result = CliRunner().invoke(steward, ["init", str(tmp_path / "sweep"), "--no-git"])

    assert result.exit_code == 0, result.output
    assert "Workspace ready" in result.output
    assert (tmp_path / "sweep" / "journal.jsonl").exists()


def test_init_defaults_to_the_current_directory(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        result = runner.invoke(steward, ["init", "--no-git"])
        assert result.exit_code == 0, result.output
        assert Workspace.find(cwd) is not None


def test_init_starts_a_repository(tmp_path: Path) -> None:
    result = CliRunner().invoke(steward, ["init", str(tmp_path / "sweep")])

    assert result.exit_code == 0, result.output
    assert "repository initialised" in result.output
    assert (tmp_path / "sweep" / ".git").exists()


def test_init_joins_an_enclosing_repository(tmp_path: Path) -> None:
    # a workspace created inside an existing project belongs to that project's
    # repository; nesting one there is a footgun rather than a convenience
    (tmp_path / ".git").mkdir()

    result = CliRunner().invoke(steward, ["init", str(tmp_path / "sweep")])

    assert result.exit_code == 0, result.output
    assert "already in a repository" in result.output
    assert not (tmp_path / "sweep" / ".git").exists()


def test_init_reports_a_complete_workspace_on_a_second_run(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(steward, ["init", str(tmp_path / "sweep"), "--no-git"])

    result = runner.invoke(steward, ["init", str(tmp_path / "sweep"), "--no-git"])

    assert result.exit_code == 0, result.output
    assert "already complete" in result.output
    assert "created" not in result.output


def test_runbook_carries_the_bounds() -> None:
    # the prohibitions are settled and are the reason an agent can be trusted
    # with the rest, so they are the part of the text pinned here
    result = CliRunner().invoke(steward, ["runbook"])

    assert result.exit_code == 0, result.output
    for bound in (
        "### What you may do",
        "### Stopping",
        "Edit the definition",
        "Writing `_steward.yaml`",
        "Move or delete a log",
        "Trust the artifact, not the exit code",
    ):
        assert bound in result.output


def test_the_rename_refusal_reaches_the_operator_as_a_message(tmp_path: Path) -> None:
    """The one migration everybody has to do must not arrive as a traceback.

    `create_workspace` refuses deliberately here — before writing anything — so
    the CLI has to translate it. Catching only `OSError` let the expected
    refusal escape as an unhandled exception.
    """
    (tmp_path / "_steward.md").write_text(
        "---\nmax_workers: 2\n---\n", encoding="utf-8"
    )

    result = CliRunner().invoke(steward, ["init", str(tmp_path)])

    assert result.exit_code == 1
    assert "_steward.md" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    # and nothing was written, which is what makes the refusal recoverable
    assert not (tmp_path / "_steward.yaml").exists()
