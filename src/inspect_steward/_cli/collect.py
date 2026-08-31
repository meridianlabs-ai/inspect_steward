"""`steward collect` — what an arriving agent reads, and where it left off.

**The one verb a transient agent needs.** Most sessions start cold: somebody opens one in the morning, or a monitor wakes one at 3am, and the agent has no memory of the night. A snapshot alone cannot serve that — it says what is true *now*, and cannot say that a task died at 1am and was respawned, or that an anomaly class grew from three instances to forty (agent.md §2.2). So this prints the snapshot **and** the stretch of history since the last collection, and records that the collection happened.

**It is `status`'s renderer with a filter, not a renderer of its own.** The three sections are the same three sections; a second renderer is how two views of one run come to disagree, which is the mistake the item type itself exists to prevent. What the filter changes is exactly two things: decisions the agent has already raised are set aside, and history starts at the cursor.

**No omission is silent.** Anything the projection sets aside is replaced by a *counted* line. An agent can read a label but cannot reason about what it was never shown, so a shortened list with nothing saying so invites it to conclude there are no open decisions when three are sitting with a human. Counting is the cheap fix; showing them is not, since an agent reading ten entries with seven marked *raised* still spends attention on all ten — which is the cost `raise` exists to remove.

**Reading is not disposing, structurally.** The cursor governs history alone. An item leaves the queue only because somebody acted on it (`ack`, `raise`), so an agent that dies mid-investigation finds its work waiting. An earlier draft asked for that as a *discipline* — read, act, then acknowledge a position — which is the kind of rule an agent forgets; nothing has to be remembered now, because there is no way to consume an item by looking at it. That is why this advances by default and `--peek` is the exception rather than the rule.

**It does not rewrite `status.md`,** for the reason in `ack.py`: the file's age is load-bearing, and a writer that is not a tend would stamp it fresh and destroy the one signal that says supervision has stopped.
"""

import click

from .._tend import status, status_markdown
from .._workspace import COLLECTED, append_event
from .turn import TURN_ERRORS, find_workspace


@click.command("collect")
@click.option(
    "--peek",
    is_flag=True,
    default=False,
    help="Read without advancing the cursor, so the next collection sees the same history again.",
)
@click.option(
    "--since",
    type=click.IntRange(min=0),
    default=None,
    help="Show history from this journal position instead of the last collection. `--since 0` shows everything.",
)
def collect_command(peek: bool, since: int | None) -> None:
    """Read what has accumulated, and mark how far you have read.

    The agent's view of the run: the decisions that are still the agent's to act on, where the run stands, and everything that has happened since the last collection. Whatever the filter sets aside is counted rather than dropped, so a shortened section never reads as an empty one.

    Advancing the cursor is a bookmark, not a pop — the journal is append-only, nothing is consumed by being read, and an open item stays open until somebody acts on it. `--peek` leaves the cursor where it is.
    """
    workspace = find_workspace()
    try:
        result = status(workspace)
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex

    # the cursor, unless the caller reached past it deliberately. A workspace no
    # agent has attached to collects from the beginning, which is the same thing
    # `--since 0` asks for and the right default for a first session
    mark = result.collected.position if result.collected is not None else 0
    click.echo(
        status_markdown(
            result,
            header=False,
            for_agent=True,
            since=since if since is not None else mark,
        )
    )

    if peek:
        return

    # the position this turn *read to*, never the one it was asked to show from:
    # `--since` reaches backwards through history that has already been
    # collected, and doing so must not un-collect what came after it
    append_event(workspace.journal, COLLECTED, position=result.position)
