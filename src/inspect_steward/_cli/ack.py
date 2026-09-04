"""`steward ack` — disposing of something nothing will fix by itself.

Most of what a turn reports goes away on its own: a park is answered, a stall starts progressing, drift is applied by a launch. What is left is the case neither projection nor lifecycle covers — a real condition nobody is going to clear mechanically, which somebody has looked at and accepted. A definition edited on purpose is the ordinary example, and without this it reports itself every ten minutes for the rest of the run, which is how an attention list stops being read.

**It takes no claim.** An acknowledgment is one append to an append-only file, and the case that matters most is the one where a tend is already in flight — an operator reading a status while the fleet converges is exactly when they decide something is fine.

**A reason is required.** The same discipline `inspect ctl` imposes on every applied change, for the same reason: this act stops something being reported at all, and *who decided, and why* has to survive it (workflow.md, *The audit trail*).

**It does not rewrite `status.md`, and that is not an oversight.** Every computed surface drops the item at once — this command, the next `status`, the next tend, the verdict, and eventually the channel — but the file is a *tend artifact whose age is load-bearing*: a remote reader detects a stopped timer, a crashed tend, or a broken sync by noticing it stopped changing (execution.md, *The reconcile core, and its drivers*). A writer that is not a tend would stamp it `as of now` and destroy the one signal that says supervision is gone. So the file catches up on the next turn, which is the same interval everything else in it is already stale by.
"""

import dataclasses
import json
from pathlib import Path

import click

from .._tend import ANOMALY, PARKED, SIGNOFF_READY, TendResult, status
from .._workspace import ACKNOWLEDGED, append_event, read_acks, read_journal
from .items import match_item
from .turn import TURN_ERRORS, find_workspace


@click.command("ack")
@click.argument("item")
@click.option(
    "--reason",
    required=True,
    help="Why this is being accepted. Recorded in the journal, and the only account of the decision that survives.",
)
@click.option(
    "--by",
    type=click.Choice(["operator", "agent"]),
    default="operator",
    show_default=True,
    help="Who decided. An agent relaying an operator's answer records `operator`; one disposing of something on its own judgement records `agent`.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the acknowledgment as JSON.",
)
def ack_command(item: str, reason: str, by: str, output_json: bool) -> None:
    """Accept an open item, so that nothing reports it again.

    `ITEM` is an item id, or any unambiguous prefix of one — ids are printed beside each item by `steward status`.

    The item leaves `status.md`, the tend summary, and the verdict; the journal keeps the record. It comes back only if the condition changes in a way that matters, because an item's id is chosen so that it does: acknowledging one edit to a definition does not acknowledge the next one.
    """
    workspace = find_workspace()
    try:
        # the current items, because only something open can be disposed of --
        # and a read is all this needs, so `status` is exactly the right caller
        result = status(workspace)
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex

    open_items = [entry for entry in result.items if entry.acknowledgeable]
    target = match_item(open_items, item)
    if target is None:
        raise click.ClickException(_nothing_matched(workspace.journal, item, result))

    append_event(
        workspace.journal,
        ACKNOWLEDGED,
        id=target.id,
        kind=target.kind,
        subject=target.subject,
        summary=target.summary,
        by=by,
        reason=reason,
    )

    if output_json:
        click.echo(
            json.dumps(
                {
                    "acknowledged": dataclasses.asdict(target),
                    "by": by,
                    "reason": reason,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"acknowledged {target.id}")
        click.echo(f"  {target.summary}")


def _nothing_matched(journal: Path, item: str, result: TendResult) -> str:
    """Why there was nothing to acknowledge, distinguishing three reasons.

    An id that matches nothing, an id acknowledged an hour ago, and an id belonging to something with no lifecycle all produce the same empty list, and they are not the same mistake. The journal answers the second, which is much the likeliest of the three.
    """
    try:
        acknowledged = read_acks(read_journal(journal).events)
    except OSError:
        acknowledged = {}

    already = [
        ack for identifier, ack in acknowledged.items() if identifier.startswith(item)
    ]
    if already:
        return f"'{item}' has already been acknowledged:\n" + "\n".join(
            f"  {ack.id} — by {ack.by} at {ack.ts}: {ack.reason}" for ack in already
        )

    named = [entry for entry in result.items if entry.id.startswith(item)]
    # the kinds that cannot be acknowledged are unacknowledgeable for different
    # reasons, and telling somebody the wrong one is worse than saying nothing:
    # a park is the most standing condition there is, and an anomaly has its
    # own closing verb
    if any(entry.kind == ANOMALY for entry in named):
        matched = next(entry for entry in named if entry.kind == ANOMALY)
        # a proposal item's subject is the proposal id, not a class key, so
        # the remediation must take the --proposal form to parse at all
        command = (
            f"steward rule --proposal {matched.subject}"
            if matched.subject.startswith("prop-")
            else f"steward rule '{matched.subject}'"
        )
        return (
            f"'{item}' is an anomaly, and an anomaly closes through a ruling "
            f"rather than an acknowledgment — `{command} "
            f"--disposition dismiss --reason ... --by ...` "
            f"is the ruling that says you looked and there is nothing here"
        )
    if any(entry.kind == SIGNOFF_READY for entry in named):
        # it *was* acknowledgeable, for as long as there was no verb to run --
        # and an ack now would record *I have decided* in the one place the
        # decision is not, leaving the run quiet with no signature, no curation
        # and nothing in anomalies.md marked final
        return (
            f"'{item}' says the results are waiting to be accepted, and "
            f"accepting them is a signature rather than an acknowledgment — "
            f"`steward signoff --by NAME` is what closes it, and it records "
            f"what was accepted"
        )
    if any(entry.kind == PARKED for entry in named):
        return (
            f"'{item}' is a worker waiting on an operator, and only answering it "
            f"clears it — acknowledging one would silence a worker that is still "
            f"holding its slot. Attach with the command `steward status` prints "
            f"beside it; `steward raise` records that you have told somebody"
        )
    if named:
        return (
            f"'{item}' is a single-turn fact rather than a standing condition, so "
            f"there is nothing to acknowledge — the next turn either finds it again "
            f"or does not"
        )
    return f"no open item matches '{item}' — `steward status` lists them with their ids"
