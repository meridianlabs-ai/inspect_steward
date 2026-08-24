from importlib import resources

import click


@click.command("runbook")
def runbook_command() -> None:
    """Print the agent runbook: how Steward works.

    The runbook ships with the package rather than living in the workspace, so an agent can never follow last year's instructions against this year's CLI. It is *mechanics*; `_steward.md` in the workspace is what a particular human wants.
    """
    click.echo(
        resources.files("inspect_steward._workspace.templates")
        .joinpath("runbook.md")
        .read_text("utf-8"),
        nl=False,
    )
