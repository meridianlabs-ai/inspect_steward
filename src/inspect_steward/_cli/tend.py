import click

from .._tend import Refused, tend
from .options import shape_options
from .turn import (
    TURN_ERRORS,
    echo_refused,
    echo_turn,
    find_workspace,
    refused_json,
    turn_json,
)


@click.command("tend")
@shape_options
@click.option(
    "--no-break-claim",
    is_flag=True,
    default=False,
    help="Refuse if another tend is wedged, rather than killing it and taking the claim.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the turn as JSON.",
)
def tend_command(
    max_workers: int | None,
    stall_after: int | None,
    samples_ramp: tuple[int, int] | bool | None,
    no_break_claim: bool,
    output_json: bool,
) -> None:
    """Run one turn of the supervision loop.

    Reconciles the log directory against the committed manifest: spawns what should be running, records what died, archives what the definition no longer asks for, then rewrites status.md and appends to the journal. Never blocks — everything long-running is a detached child that a later turn observes.

    Safe to call as often as you like. A repeated turn is a no-op, and an interrupted one is reconciled by the next.
    """
    workspace = find_workspace()
    try:
        result = tend(
            workspace,
            max_workers=max_workers,
            stall_after=stall_after,
            samples_ramp=samples_ramp,
            break_stale=not no_break_claim,
        )
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex

    if isinstance(result, Refused):
        if output_json:
            click.echo(refused_json(result))
        else:
            echo_refused(result)
        return

    if output_json:
        click.echo(turn_json(result))
    else:
        echo_turn(result)
