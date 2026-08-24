import click

from .._tend import status
from .turn import TURN_ERRORS, echo_turn, find_workspace, turn_json


@click.command("status")
@click.option(
    "--max-workers",
    type=click.IntRange(min=1),
    default=None,
    help="Ceiling to preview against (overrides _steward.md).",
)
@click.option(
    "--max-samples",
    type=click.IntRange(min=1),
    default=None,
    help="Sample concurrency to preview against (overrides the definition).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the state as JSON.",
)
def status_command(
    max_workers: int | None, max_samples: int | None, output_json: bool
) -> None:
    """Report where the run stands, and what the next turn would do.

    `tend --dry-run`: the same reads and the same decision, with the actions discarded. Read-only — it spawns nothing, moves nothing, writes nothing, and does not take the run claim, so it is safe to run as often as you like while a tend is in flight.
    """
    workspace = find_workspace()
    try:
        result = status(workspace, max_workers=max_workers, max_samples=max_samples)
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex

    if output_json:
        click.echo(turn_json(result))
    else:
        echo_turn(result)
