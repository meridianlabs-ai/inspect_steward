import click

from .._tend import status, status_markdown
from .options import shape_options
from .turn import TURN_ERRORS, echo_turn, find_workspace, turn_json


@click.command("status")
@shape_options
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "md"]),
    default="text",
    help="`text` for a terminal; `md` for an agent relaying this to somebody, which is what agent.md asks it to do verbatim.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the state as JSON.",
)
def status_command(
    max_workers: int | None,
    stall_after: int | None,
    samples_ramp: tuple[int, int] | bool | None,
    stuck_after: int | None,
    preauthorized: dict[str, str] | bool | None,
    output_format: str,
    output_json: bool,
) -> None:
    """Report where the run stands, and what the next turn would do.

    `tend --dry-run`: the same reads and the same decision, with the actions discarded. Read-only — it spawns nothing, moves nothing, writes nothing, and does not take the run claim, so it is safe to run as often as you like while a tend is in flight.
    """
    workspace = find_workspace()
    try:
        result = status(
            workspace,
            max_workers=max_workers,
            stall_after=stall_after,
            samples_ramp=samples_ramp,
            stuck_after=stuck_after,
            preauthorized=preauthorized,
        )
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex

    if output_json:
        click.echo(turn_json(result))
    elif output_format == "md":
        # the operator's page, the same renderer `status.md` uses, minus its
        # generated-file comment: an agent is told to relay this in full and
        # unfenced (runbook, *When the operator asks how it is going*), and a
        # warning about editing a file is not part of what it was asked to relay
        click.echo(status_markdown(result, header=False))
    else:
        echo_turn(result)
