"""`steward note` — writing something down where the next reader will find it.

The journal is the only record one session leaves that another session reads, and two acts leave no entry of their own: stopping to ask, where `notify` posts to a channel and writes nothing, and reaching into a worker through `inspect ctl`, where the change is recorded in the eval log and no `collect` looks. This is the verb for both — one append, shown under *what happened*, so a 6am reader and the next session see the state and the hypothesis at the moment they were formed.

**It takes no claim and changes nothing.** A note is not a decision: it opens nothing, closes nothing, and pauses nothing. The verbs that do those things carry their own `--reason`.
"""

import click

from .._workspace import NOTED, append_event
from .turn import find_workspace


@click.command("note")
@click.argument("message")
@click.option(
    "--by",
    type=click.Choice(["human", "agent"]),
    default="agent",
    show_default=True,
    help="Whose note. Defaults to the agent, whose observations are what this verb exists to keep.",
)
def note_command(message: str, by: str) -> None:
    """Write a note into the journal, for whoever reads this run next.

    MESSAGE is free text: the state of something and what you think it means. It appears under *what happened* in `status` and `collect`, in order with everything else that was done to the run.
    """
    workspace = find_workspace()
    text = message.strip()
    if not text:
        raise click.ClickException("a note needs some text")
    append_event(workspace.journal, NOTED, by=by, text=text)
    click.echo("noted")
