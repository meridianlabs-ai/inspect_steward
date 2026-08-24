from click.testing import CliRunner
from inspect_steward._cli.main import steward


def test_cli_help_lists_commands() -> None:
    result = CliRunner().invoke(steward, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "runbook", "status", "tasks", "tend"):
        assert command in result.output


def test_cli_version() -> None:
    result = CliRunner().invoke(steward, ["--version"])
    assert result.exit_code == 0
    assert "steward" in result.output


# `init` itself is covered in tests/workspace/test_init_cli.py, where every
# invocation is given a directory. Invoking it here without one wrote a
# workspace into the repository -- `init` was right, the test was not.
