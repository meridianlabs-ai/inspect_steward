"""`steward propose` — the agent's grouping judgement, put in front of an operator.

A night's failures are usually two or three causes wearing many class keys, and the proposal is the layer that says *these are one decision*: one action, the covered classes, and per-class evidence **snapshotted from the fold** — so the record shows what the operator was shown, and so they can answer part of it (`steward rule --proposal ID CLASS` rules just that class; the remainder stays proposed).

Optional ceremony, never a gate: with no agent, classes stand alone and `steward rule CLASS` works bare (workflow.md §12.4). Re-proposing a class supersedes its earlier coverage, loudly.
"""

import json
from typing import Any

import click

from .._anomaly.model import Anomalies, Disposition
from .._evalset.classify import digest8
from .._tend import status
from .._workspace import PROPOSAL, append_event
from .anomalies import (
    match_class,
    open_classes,
    persist_windows,
    precedent_lines,
    refuse_dishonest,
    settled_ruling,
)
from .turn import TURN_ERRORS, find_workspace


@click.command("propose")
@click.argument("classes", nargs=-1, required=True)
@click.option(
    "--action",
    required=True,
    type=click.Choice([disposition.value for disposition in Disposition]),
    help="The one disposition this proposal asks for. Classes wanting different answers are different proposals.",
)
@click.option(
    "--reason",
    required=True,
    help="Why these classes are one decision — what the investigation found.",
)
@click.option(
    "--by",
    default="agent",
    show_default=True,
    help="Who is proposing.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the proposal as JSON.",
)
def propose_command(
    classes: tuple[str, ...],
    action: str,
    reason: str,
    by: str,
    output_json: bool,
) -> None:
    """Propose one disposition for one or more anomaly classes.

    `CLASSES` are open class keys, or unambiguous prefixes. The proposal becomes one consolidated item for its owner, answered whole or in part by `steward rule --proposal ID`.
    """
    workspace = find_workspace()
    try:
        result = status(workspace)
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex
    anomalies = result.anomalies

    decided = Disposition(action)
    # deduped, because two prefixes naming one class are one covered class --
    # and because duplicates would shift the id's digest
    targets = list(dict.fromkeys(_matched(anomalies, token) for token in classes))
    _refused(anomalies, targets, decided)

    # the generation is digest material: the same classes re-proposed after a
    # ruling and a recurrence are a new question, and the id must be new for
    # the appeared-diff and the raised fold to say so
    stamped = ",".join(
        f"{key}#g{_generation(anomalies, key)}" for key in sorted(targets)
    )
    identifier = f"prop-{digest8(stamped + ':' + decided.value)}"
    superseded = _superseded(anomalies, targets, identifier)
    evidence = {key: _snapshot(anomalies, key) for key in targets}
    persist_windows(workspace.journal, result.anomaly_pending, targets)
    append_event(
        workspace.journal,
        PROPOSAL,
        id=identifier,
        action=decided.value,
        classes=evidence,
        reason=reason,
        by=by,
    )

    if output_json:
        click.echo(
            json.dumps(
                {
                    "proposal": identifier,
                    "action": decided.value,
                    "classes": evidence,
                    "reason": reason,
                    "by": by,
                },
                indent=2,
            )
        )
        return
    click.echo(f"proposed {identifier}: {decided.value} — {reason}")
    for key, snapshot in evidence.items():
        click.echo(f"  {key} ({snapshot['count']} instances)")
        for line in snapshot.get("precedent", []):
            click.echo(f"    precedent: {line}")
    for key, earlier in superseded.items():
        click.echo(f"  supersedes {earlier} for {key}")
    click.echo(
        f"answer with: steward rule --proposal {identifier} --reason ... --by ..."
    )


def _generation(anomalies: Anomalies, key: str) -> int:
    return max(
        (anomaly.generation for anomaly in anomalies.open if anomaly.class_key == key),
        default=1,
    )


def _matched(anomalies: Anomalies, token: str) -> str:
    matched = match_class(open_classes(anomalies), token)
    if matched is not None:
        return matched
    settled = settled_ruling(anomalies, token)
    if settled is not None:
        raise click.ClickException(
            f"'{token}' is already settled — ruled {settled.disposition.value} "
            f"by {settled.by} at {settled.ts}. There is nothing to propose"
        )
    keys = open_classes(anomalies)
    listed = "\n".join(f"  {key}" for key in keys) if keys else "  (none are open)"
    raise click.ClickException(f"no open class matches '{token}' — open now:\n{listed}")


def _refused(anomalies: Anomalies, targets: list[str], decided: Disposition) -> None:
    """What may not even be proposed.

    The same disposition matrix `rule` applies — proposing what cannot be ruled would put an unanswerable question in front of an operator — plus the substrate gate: a substrate-flagged class gets no re-run proposal until an operator has looked, because re-running into broken machinery burns the work twice (execution.md §9.1). An operator's direct `rule rerun` *is* that look; an agent's proposal is not.
    """
    refuse_dishonest(targets, decided)
    if decided is Disposition.RERUN:
        flagged = sorted(
            {
                anomaly.class_key
                for anomaly in anomalies.open
                if anomaly.class_key in targets and anomaly.substrate
            }
        )
        if flagged:
            raise click.ClickException(
                f"{', '.join(flagged)} looks like the machinery under the run, "
                f"and a re-run into a broken substrate burns the work twice — "
                f"verify storage first; an operator ruling rerun directly is that "
                f"verification"
            )


def _superseded(
    anomalies: Anomalies, targets: list[str], identifier: str
) -> dict[str, str]:
    """Classes already covered by a different live proposal, which this one takes over."""
    return {
        anomaly.class_key: anomaly.proposal
        for anomaly in anomalies.open
        if anomaly.class_key in targets
        and anomaly.proposal is not None
        and anomaly.proposal != identifier
    }


def _snapshot(anomalies: Anomalies, key: str) -> dict[str, Any]:
    windows = [anomaly for anomaly in anomalies.open if anomaly.class_key == key]
    count = sum(window.evidence.count for window in windows)
    newest = max(windows, key=lambda anomaly: anomaly.generation)
    snapshot: dict[str, Any] = {
        "count": count,
        "exemplar": newest.evidence.exemplar,
        "first_ts": min(
            (w.evidence.first_ts for w in windows if w.evidence.first_ts), default=""
        ),
        "last_ts": max((w.evidence.last_ts for w in windows), default=""),
    }
    precedent = precedent_lines(newest)
    if precedent:
        snapshot["precedent"] = precedent
    return snapshot
