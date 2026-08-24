import click

from .._evalset.detect import DefinitionType
from .._workspace import CreateReport, Outcome, create_workspace


@click.command("init")
@click.argument(
    "directory",
    type=click.Path(file_okay=False),
    default=".",
    required=False,
)
@click.option(
    "--type",
    "definition_type",
    type=click.Choice(["evalset", "flow", "hawk"]),
    default="evalset",
    help="Definition type, which decides the placeholder's filename.",
)
@click.option(
    "--no-git",
    "no_git",
    is_flag=True,
    default=False,
    help="Do not initialise a git repository.",
)
def init_command(directory: str, definition_type: DefinitionType, no_git: bool) -> None:
    """Create a steward workspace.

    DIRECTORY defaults to the current directory and is created if it does not exist.

    Safe to re-run: existing files are kept and only what is missing is added. Steward never overwrites your work — least of all `_steward.md`, which it proposes changes to but never writes.
    """
    try:
        report = create_workspace(directory, type=definition_type, git=not no_git)
    except OSError as ex:
        raise click.ClickException(f"Unable to create the workspace: {ex}") from ex

    _print_report(report)


_MARKS = {
    Outcome.CREATED: "+",
    Outcome.UPDATED: "~",
    Outcome.KEPT: "=",
    Outcome.SKIPPED: "-",
}


def _print_report(report: CreateReport) -> None:
    width = max(len(step.path) for step in report.steps)
    for step in report.steps:
        detail = f"  {step.detail}" if step.detail else ""
        click.echo(
            f"  {_MARKS[step.outcome]} {step.path:<{width}}  {step.outcome.value}{detail}"
        )

    workspace = report.workspace
    if report.created_anything:
        click.echo(f"\nWorkspace ready at {workspace.root}")
        definition = workspace.find_definition()
        if definition is not None and definition.stat().st_size == 0:
            click.echo(f"Write your eval set in {definition.name}, then run:")
            click.echo(f"  steward tasks {definition.name}")
    else:
        click.echo(f"\nWorkspace already complete at {workspace.root}")
