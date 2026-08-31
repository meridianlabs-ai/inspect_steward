"""`steward rule` — deciding what a class of failures means.

The verb the whole anomaly machinery exists to reach: five hundred errored samples become one class, the class becomes one question, and this is where a person answers it — with a disposition, a required reason, and their name. One `ruling` event lands per class, sharing a proposal id when the decision answered one, which is what lets a group decision be unpicked later (workflow.md §5.6).

**It takes no claim**, for the same reason `ack` does not: a ruling is one append to an append-only file, and the moment that matters most is a person reading a status while a tend is in flight.

**`--by` is free text naming a person, never a role.** A ruling is never the agent's own (agent.md §6); an agent relaying a decision records who decided.
"""

import json
from typing import Any

import click

from .._anomaly.model import Anomalies, Anomaly, Disposition, composed_effect
from .._tend import status
from .._workspace import RULING, append_event
from .anomalies import (
    match_class,
    open_classes,
    persist_windows,
    precedent_lines,
    refuse_dishonest,
    settled_ruling,
)
from .turn import TURN_ERRORS, find_workspace


@click.command("rule")
@click.argument("classes", nargs=-1)
@click.option(
    "--proposal",
    "proposal_id",
    default=None,
    help="Answer a proposal by id. Alone, rules every class it covers that still awaits one; with CLASS arguments, rules just those — a partial answer, and the remainder stays proposed.",
)
@click.option(
    "--disposition",
    type=click.Choice([disposition.value for disposition in Disposition]),
    default=None,
    help="The answer. Required unless `--proposal` supplies it; given with one, it overrides for the named classes.",
)
@click.option(
    "--reason",
    required=True,
    help="Why. Recorded in the journal, attached as precedent to any recurrence, and the only account of the decision that survives.",
)
@click.option(
    "--by",
    required=True,
    help="Who decided — a name, never a role. An agent relaying a person's decision records the person.",
)
@click.option(
    "--effect",
    default=None,
    help="The sentence the report carries for a disposition that marks the data. Composed automatically for exclude/zero/score, required for accept, refused for rerun and dismiss — which mark nothing.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the rulings as JSON.",
)
def rule_command(
    classes: tuple[str, ...],
    proposal_id: str | None,
    disposition: str | None,
    reason: str,
    by: str,
    effect: str | None,
    output_json: bool,
) -> None:
    """Rule on anomaly classes: what the failures mean, and what happens to the data.

    `CLASSES` are class keys as `steward status` prints them, or any unambiguous prefix. A ruling closes the class's window — every open generation of it — and recurrence afterwards opens a new one carrying this decision as precedent.
    """
    workspace = find_workspace()
    try:
        result = status(workspace)
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex
    anomalies = result.anomalies

    if proposal_id is not None:
        targets, decided = _from_proposal(anomalies, proposal_id, classes, disposition)
    else:
        if not classes:
            raise click.ClickException(
                "name at least one class, or answer a proposal with --proposal ID"
            )
        if disposition is None:
            raise click.ClickException(
                "--disposition is required — one of "
                + ", ".join(entry.value for entry in Disposition)
            )
        # deduped, because two prefixes naming one class must land one ruling,
        # not a ruling immediately superseded by its own copy
        targets = list(dict.fromkeys(_matched(anomalies, token) for token in classes))
        decided = Disposition(disposition)

    refuse_dishonest(targets, decided)
    effects = _effects(anomalies, targets, decided, effect)

    persist_windows(workspace.journal, result.anomaly_pending, targets)
    for key in targets:
        fields: dict[str, Any] = {
            "class": key,
            "disposition": decided.value,
            "reason": reason,
            "by": by,
            "effect": effects.get(key, ""),
        }
        if proposal_id is not None:
            fields["proposal"] = proposal_id
        append_event(workspace.journal, RULING, **fields)

    if output_json:
        click.echo(
            json.dumps(
                {
                    "ruled": [
                        {
                            "class": key,
                            "disposition": decided.value,
                            "reason": reason,
                            "by": by,
                            "effect": effects.get(key, ""),
                            "proposal": proposal_id,
                        }
                        for key in targets
                    ]
                },
                indent=2,
            )
        )
        return
    for key in targets:
        click.echo(f"ruled {key}: {decided.value} — {reason} (by {by})")
        if effects.get(key):
            click.echo(f"  effect: {effects[key]}")
        for window in _open_windows(anomalies, key):
            standing = window.ruling
            if window.state.value == "ruled" and standing is not None:
                # loudly, because the standing decision was somebody else's
                click.echo(
                    f"  supersedes the standing {standing.disposition.value} "
                    f"ruling by {standing.by} at {standing.ts}"
                )
            for line in precedent_lines(window):
                click.echo(f"  precedent: {line}")


def _from_proposal(
    anomalies: Anomalies,
    proposal_id: str,
    classes: tuple[str, ...],
    disposition: str | None,
) -> tuple[list[str], Disposition]:
    """The classes a proposal answer covers, and the disposition it lands."""
    proposal = anomalies.proposals.get(proposal_id)
    if proposal is None:
        live = ", ".join(sorted(anomalies.proposals)) or "none are live"
        raise click.ClickException(
            f"no live proposal '{proposal_id}' — {live}. A proposal already "
            f"fully answered is no longer live; its classes rule directly"
        )
    # what still stands proposed *under this proposal* — a class already ruled
    # through it is not re-ruled by answering the remainder, one pulled out by
    # an investigation is back in the agent's hands, and one superseded into a
    # later proposal answers there; superseding a standing ruling takes naming
    # the class directly
    covered = [
        key
        for key in proposal.classes
        if (window := anomalies.absorbing(key)) is not None
        and window.proposal == proposal_id
    ]
    if not classes:
        return covered, (
            Disposition(disposition) if disposition is not None else proposal.action
        )
    targets: list[str] = []
    for token in classes:
        matched = match_class(covered, token)
        if matched is None:
            raise click.ClickException(
                f"'{token}' is not covered by {proposal_id}, which covers:\n"
                + "\n".join(f"  {key}" for key in covered)
            )
        if matched not in targets:
            targets.append(matched)
    return targets, (
        Disposition(disposition) if disposition is not None else proposal.action
    )


def _matched(anomalies: Anomalies, token: str) -> str:
    matched = match_class(open_classes(anomalies), token)
    if matched is not None:
        return matched
    settled = settled_ruling(anomalies, token)
    if settled is not None:
        raise click.ClickException(
            f"'{token}' is already settled — ruled {settled.disposition.value} "
            f"by {settled.by} at {settled.ts}: {settled.reason}. Recurrence "
            f"opens a new window; nothing is open now"
        )
    keys = open_classes(anomalies)
    listed = "\n".join(f"  {key}" for key in keys) if keys else "  (none are open)"
    raise click.ClickException(f"no open class matches '{token}' — open now:\n{listed}")


def _effects(
    anomalies: Anomalies,
    targets: list[str],
    decided: Disposition,
    effect: str | None,
) -> dict[str, str]:
    """The report-facing sentence per class, under the per-disposition rules."""
    if decided in (Disposition.RERUN, Disposition.DISMISS):
        if effect is not None:
            raise click.ClickException(
                f"{decided.value} marks nothing in the data, so --effect has "
                f"nothing to attach to"
            )
        return {}
    if decided is Disposition.ACCEPT:
        if effect is None:
            raise click.ClickException(
                "accept requires --effect: the sentence the report carries "
                "for data that stands with a caveat"
            )
        return {key: effect for key in targets}
    if effect is not None:
        return {key: effect for key in targets}
    # the shared composition, so a person's ruling and a policy's cannot word
    # the same mark differently
    return {key: composed_effect(anomalies, key, decided) for key in targets}


def _open_windows(anomalies: Anomalies, key: str) -> list[Anomaly]:
    return [anomaly for anomaly in anomalies.open if anomaly.class_key == key]
