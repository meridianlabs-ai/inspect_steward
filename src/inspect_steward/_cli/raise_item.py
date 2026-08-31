"""`steward raise` — the agent saying it has done its part.

**The state between open and closed, and the one an item list is easy to build without.** An item owned by a human stays open until a human rules on it. But the agent's work on it — noticing it, investigating it, putting it in front of somebody — ends much earlier, and with nothing recording that, the item comes back at every collection all night. Sixty appearances, the same drift, and no way for the agent to get it out of its way except by acknowledging something that is not its to acknowledge (agent.md §2.2).

**It closes nothing.** `status` still shows a raised item, in full, because a person still owes an answer; the verdict still counts it. The only projection that changes is the agent's own, where it is set aside and *counted* rather than hidden — an agent can read a label but cannot reason about what it was never shown.

**Which is exactly why only a human-owned item can be raised.** Taking something out of the agent's queue without closing it is safe only where somebody else is going to close it. An agent-owned item raised this way is stranded: open forever, gone from the queue that would have brought it back, and owned by nobody who is looking at it. The agent's verb for its own work is `ack --by agent`, after it has actually resolved the thing, and the two are not interchangeable — one records a hand-off and the other records a disposal.

**The note is optional, where `ack --reason` is required.** Disposing of a decision owes an account that has to survive the item; handing one off does not. Forcing a note here would manufacture one per hand-off, most of them saying nothing, which is how a required field stops being read.

Named `raise_item` because `raise` is a keyword. Everything a person types says `raise`.
"""

import dataclasses
import json
from pathlib import Path

import click

from .._tend import Owner, TendResult, status
from .._workspace import (
    RAISED,
    JournalEvent,
    append_event,
    read_journal,
    read_raised,
)
from .items import match_item
from .turn import TURN_ERRORS, find_workspace


@click.command("raise")
@click.argument("item")
@click.option(
    "--note",
    default="",
    help="What was done to surface it — where it was asked, and of whom. Optional: handing a decision off does not owe the account that disposing of one does.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the hand-off as JSON.",
)
def raise_command(item: str, note: str, output_json: bool) -> None:
    """Record that an item is now with the person who can decide it.

    `ITEM` is a **human-owned** item's id, or any unambiguous prefix of one — ids are printed beside each item by `steward status`, under the heading that says whose it is. An item the agent owns is its own to investigate and then `steward ack --by agent`; raising one would take it out of the agent's queue with nobody else looking at it.

    The item stays open and stays in `status`: only a ruling closes it. What changes is that `steward collect` stops offering it as work, so the agent is not shown the same decision every time it looks. It returns if the condition changes in a way that matters, because an item's id is chosen so that it does.
    """
    workspace = find_workspace()
    try:
        result = status(workspace)
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex

    # **owned by a human, and that is the whole gate.** Raising takes an item out
    # of the agent's queue without closing it, which is only safe where somebody
    # *else* is going to close it. Applied to an agent-owned item it is a dead
    # end: nothing will ever bring it back, and the agent has silenced its own
    # outstanding work by declaring itself finished with it (agent.md §2.2).
    #
    # Ownership alone, and *not* `acknowledgeable` as well. That half was
    # standing in for this one: it was there to keep `action_failed` out, and
    # `action_failed` is the agent's, so the owner gate already excludes it.
    # Keeping it stopped mattering once a kind was both human-owned and
    # unacknowledgeable -- a park, which cannot be acked because only answering
    # clears it, and would then have been unraisable too, sitting in the agent's
    # queue at every collection all night. Which is the precise failure `raise`
    # exists to prevent.
    open_items = [entry for entry in result.items if entry.owner is Owner.HUMAN]
    target = match_item(open_items, item)
    if target is None:
        raise click.ClickException(_nothing_matched(workspace.journal, item, result))

    # **an item already raised is still nameable, and raising it again appends
    # rather than being refused.** Chasing a decision a second time is real
    # work: the fold keeps the last word, so the newer note is the one a reader
    # sees, and the record of both attempts stays in *what happened*
    before = read_raised(_events(workspace.journal)).get(target.id)

    append_event(
        workspace.journal,
        RAISED,
        id=target.id,
        kind=target.kind,
        subject=target.subject,
        summary=target.summary,
        note=note,
    )

    if output_json:
        click.echo(
            json.dumps({"raised": dataclasses.asdict(target), "note": note}, indent=2)
        )
    else:
        click.echo(f"raised {target.id}")
        click.echo(f"  {target.summary}")
        if before is not None:
            click.echo(f"  already raised at {before.ts}; nobody has ruled on it yet")
        else:
            click.echo("  still open — a person decides it; `steward collect` will not")


def _events(journal: Path) -> list[JournalEvent]:
    """The journal, or an empty history where it cannot be read.

    Nothing here is load-bearing enough to fail a hand-off over: what it decides is which of two sentences gets printed after the event has already landed.
    """
    try:
        return read_journal(journal).events
    except OSError:
        return []


def _nothing_matched(journal: Path, item: str, result: TendResult) -> str:
    """Why there was nothing to raise, distinguishing the three reasons.

    The same shape as `ack`'s, and deliberately not shared with it: *already raised* and *already acknowledged* are different facts about an item and the message is the whole value of the function.

    Three rather than four now that the gate is ownership alone: every human-owned item is raisable, so *matched but not offered* can only mean the agent's own.
    """
    raised = read_raised(_events(journal))

    already = [
        entry for identifier, entry in raised.items() if identifier.startswith(item)
    ]
    if already:
        # raising does not close anything, so reaching here means the item is
        # gone for some *other* reason -- somebody ruled on it, or the condition
        # cleared. Either way the hand-off already happened and is over
        return f"'{item}' is no longer open, and was raised:\n" + "\n".join(
            f"  {entry.id} — at {entry.ts}" + (f": {entry.note}" if entry.note else "")
            for entry in already
        )

    named = [entry for entry in result.items if entry.id.startswith(item)]
    if agent_owned := [entry for entry in named if entry.owner is Owner.AGENT]:
        # the mistake worth naming precisely, because raising it would *work* in
        # the sense of clearing the queue and would leave the item stranded:
        # open forever, invisible to the agent, and owned by nobody who is
        # looking. Investigation is the act here, and `ack` is how it ends
        return (
            f"'{item}' is the agent's own to resolve rather than a person's, so "
            f"there is nobody to hand it to:\n"
            + "\n".join(f"  {entry.id} — {entry.summary}" for entry in agent_owned)
            + "\n\ninvestigate it, then `steward ack --by agent --reason ...` to "
            "record what you found"
        )
    return f"no open item matches '{item}' — `steward status` lists them with their ids"
