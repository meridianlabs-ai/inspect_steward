from click.testing import CliRunner

from inspect_steward._cli.main import steward


def test_cli_help_lists_init() -> None:
    result = CliRunner().invoke(steward, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output


def test_cli_version() -> None:
    result = CliRunner().invoke(steward, ["--version"])
    assert result.exit_code == 0
    assert "steward" in result.output


def test_cli_init() -> None:
    result = CliRunner().invoke(steward, ["init"])
    assert result.exit_code == 0
