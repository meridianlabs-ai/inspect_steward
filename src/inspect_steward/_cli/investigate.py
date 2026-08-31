"""`steward investigate` — marking a class as being worked, before anything is proposed.

The record that survives a session boundary: the next agent must not re-open or re-propose what the last one was mid-way through, and `status` must be able to say a class is in hand (workflow.md §12.5). The note is the hand-off — what has been looked at, what remains.
"""

from typing import Any

import click

from .._anomaly.model import Anomalies
from .._tend import status
from .._workspace import INVESTIGATING, append_event
from .anomalies import match_class, open_classes, persist_windows, settled_ruling
from .turn import TURN_ERRORS, find_workspace


@click.command("investigate")
@click.argument("class_key", metavar="CLASS")
@click.option(
    "--note",
    required=True,
    help="Where the investigation stands — written for the next session, not this one.",
)
@click.option(
    "--by",
    default="agent",
    show_default=True,
    help="Who is investigating.",
)
def investigate_command(class_key: str, note: str, by: str) -> None:
    """Mark an anomaly class as under investigation.

    `CLASS` is an open class key as `steward status` prints it, or any unambiguous prefix. Investigating a proposed class pulls it back out of its proposal.
    """
    workspace = find_workspace()
    try:
        result = status(workspace)
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex

    matched = _matched(result.anomalies, class_key)
    persist_windows(workspace.journal, result.anomaly_pending, [matched])
    fields: dict[str, Any] = {"class": matched, "by": by, "note": note}
    append_event(workspace.journal, INVESTIGATING, **fields)
    click.echo(f"investigating {matched}")
    click.echo(f"  {note}")


def _matched(anomalies: Anomalies, token: str) -> str:
    matched = match_class(open_classes(anomalies), token)
    if matched is not None:
        return matched
    settled = settled_ruling(anomalies, token)
    if settled is not None:
        raise click.ClickException(
            f"'{token}' is already settled — ruled {settled.disposition.value} "
            f"by {settled.by} at {settled.ts}. There is nothing to investigate"
        )
    keys = open_classes(anomalies)
    listed = "\n".join(f"  {key}" for key in keys) if keys else "  (none are open)"
    raise click.ClickException(f"no open class matches '{token}' — open now:\n{listed}")
