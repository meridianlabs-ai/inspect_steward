"""`steward ramp hold` and `steward ramp resume` — the agent's brake on the tuning loop.

The loop climbs on its own (`_tend.tuning`), so intervening has to be one cheap act rather than an argument with a timer: a hold freezes climbing at the current levels and the loop's safety half stays armed — a storm still cuts the connection ceiling, because the cut exists precisely for when nobody is watching.

The same shape as `pause`/`resume`, for the same reasons. **Neither takes the claim** — both are one append to the journal, and the moment somebody most wants to hold is the moment a tend is mid-turn ramping the thing they want held; a turn already running finishes, and the next one reads the latch. And the journal rather than `.steward/`, because a hold that a cleared cache silently released would resume climbing into whatever the holder saw coming.
"""

from pathlib import Path

import click

from .._evalset.manifest import read_manifest
from .._workspace import (
    RAMP_HELD,
    RAMP_RESUMED,
    RampHold,
    Workspace,
    append_event,
    read_journal,
    read_ramp_holds,
)
from .turn import find_workspace


@click.group("ramp")
def ramp_command() -> None:
    """Hold or resume the tuning loop's climb."""


@ramp_command.command("hold")
@click.argument("identifier", required=False)
@click.option(
    "--reason",
    required=True,
    help="Why the climb is being held. Recorded in the journal, and what the next reader of the tuning block sees.",
)
@click.option(
    "--by",
    type=click.Choice(["human", "agent"]),
    default="agent",
    show_default=True,
    help="Who decided. Defaults to the agent, because holding on its own judgement is exactly what this verb exists for.",
)
def hold_command(identifier: str | None, reason: str, by: str) -> None:
    """Stop the tuning loop climbing, leaving the levels where they are.

    With IDENTIFIER (a task identifier, from `steward tasks`), holds that one arm and leaves the others climbing; bare, holds the fleet. Ramp-downs stay active either way — a hold is a brake on growth, never on the cut that exits a retry storm.
    """
    workspace = find_workspace()
    if identifier is not None:
        _known(workspace, identifier)
    holds = _holds(workspace.journal)
    key = identifier or ""
    if (current := holds.get(key)) is not None:
        what = f"the ramp on {identifier}" if identifier else "the ramp"
        raise click.ClickException(
            f"{what} is already held by {current.by or 'somebody'} at {current.ts}: "
            f"{current.reason or 'no reason recorded'}"
        )

    append_event(
        workspace.journal, RAMP_HELD, by=by, reason=reason, identifier=identifier or ""
    )
    what = identifier or "the fleet"
    click.echo(f"⏸ ramp held for {what} — levels stay where they are")
    click.echo("  `steward ramp resume` lets it climb again")


@ramp_command.command("resume")
@click.argument("identifier", required=False)
def resume_command(identifier: str | None) -> None:
    """Let the tuning loop climb again.

    With IDENTIFIER, releases that task's hold; bare, releases everything — the fleet-wide hold and every per-task one, because the bare verb means *ramp freely again* rather than *ramp except where I have forgotten I said otherwise*.
    """
    workspace = find_workspace()
    holds = _holds(workspace.journal)
    if identifier and identifier not in holds:
        raise click.ClickException(f"the ramp on {identifier} is not held")
    if not identifier and not holds:
        raise click.ClickException("the ramp is not held")

    append_event(workspace.journal, RAMP_RESUMED, identifier=identifier or "")
    click.echo("ramp resumed — the next tend may climb where the window is clean")


def _known(workspace: Workspace, identifier: str) -> None:
    """Refuse an identifier the manifest does not name.

    The hold matches on an exact identifier, so a typo holds nothing while printing that it did — and the arm the person was worried about keeps climbing on the strength of a message saying it would not. That is the one failure this verb must not have, because a hold is reached for precisely when somebody has stopped trusting the loop to be right.

    Raises:
        click.ClickException: If the identifier names no task in the manifest, or the manifest cannot be read.
    """
    try:
        identifiers = {
            task.identifier for task in read_manifest(workspace.manifest).tasks
        }
    except (OSError, ValueError) as ex:
        raise click.ClickException(
            f"the manifest could not be read, so '{identifier}' cannot be "
            f"checked against it: {ex}"
        ) from ex
    if identifier not in identifiers:
        raise click.ClickException(
            f"no task in this run is called '{identifier}' — `steward tasks` "
            f"lists them, and a bare `steward ramp hold` holds the whole fleet"
        )


def _holds(journal: Path) -> dict[str, RampHold]:
    """The holds in force.

    An unreadable journal is reported here rather than swallowed: both commands are about to append to it, and failing at the read with a message naming the file is better than failing at the write.
    """
    try:
        return read_ramp_holds(read_journal(journal).events)
    except OSError as ex:
        raise click.ClickException(f"the journal could not be read: {ex}") from ex
