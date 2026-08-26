"""`steward pause` and `steward resume` — the brake the timer creates the need for.

Before there was a timer, *not tending* was pausing. After one, a fleet reconverges every ten minutes whether or not anybody wants it to, and there has to be a way to say stop that does not mean uninstalling the supervision.

**Pausing stops scheduling, and only that.** No new workers, no archiving — a paused run makes no changes to itself. Work already in flight finishes normally, which is what almost everyone means by pausing a run and which needs no control channel at all. Suspending a running worker is a different act, it needs the control channel, and it is something a ruling authorizes (workflow.md, *What `pause` actually pauses*).

**Neither takes the claim.** Both are one append to an append-only file, and the moment somebody most wants to pause is the moment a tend is in flight spawning the workers they want stopped. A tend already running finishes its turn; the next one reads the flag.
"""

from pathlib import Path

import click

from .._workspace import (
    PAUSED,
    RESUMED,
    Paused,
    append_event,
    read_journal,
    read_pause,
)
from .turn import find_workspace


@click.command("pause")
@click.option(
    "--reason",
    required=True,
    help="Why the run is being held. Recorded in the journal, and the only account of the decision that survives.",
)
@click.option(
    "--by",
    type=click.Choice(["human", "agent"]),
    default="human",
    show_default=True,
    help="Who decided. An agent relaying a person's instruction records `human`.",
)
def pause_command(reason: str, by: str) -> None:
    """Stop scheduling new work, leaving what is running to finish.

    Every later turn reports the run as paused and spawns nothing. Workers already in flight are left alone: stopping one is not a mechanical act, and it is not what pausing means.

    Recorded in the journal rather than in `.steward/`, which is disposable — a pause that a cleared cache silently undid would resume an expensive run with nobody watching.
    """
    workspace = find_workspace()
    if (current := _current(workspace.journal)) is not None:
        raise click.ClickException(
            f"already paused by {current.by or 'somebody'} at {current.ts}: "
            f"{current.reason or 'no reason recorded'}"
        )

    append_event(workspace.journal, PAUSED, by=by, reason=reason)
    click.echo("⏸ paused — nothing new will be scheduled")
    click.echo("  `steward resume` starts scheduling again")


@click.command("resume")
def resume_command() -> None:
    """Start scheduling again.

    The next tend converges from whatever it finds, which is not necessarily where the run was when it was paused — logs landed, workers exited, and the definition may have been relaunched. That is the ordinary behaviour of the loop rather than a caveat about pausing.
    """
    workspace = find_workspace()
    if _current(workspace.journal) is None:
        raise click.ClickException("this run is not paused")

    append_event(workspace.journal, RESUMED)
    click.echo("resumed — the next tend will schedule normally")


def _current(journal: Path) -> Paused | None:
    """The pause in force.

    An unreadable journal is reported here rather than swallowed: both commands are about to append to it, and failing at the read with a message naming the file is better than failing at the write.
    """
    try:
        return read_pause(read_journal(journal).events)
    except OSError as ex:
        raise click.ClickException(f"the journal could not be read: {ex}") from ex
