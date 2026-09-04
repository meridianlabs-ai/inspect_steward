"""`steward rule` — deciding what a class of failures means.

The verb the whole anomaly machinery exists to reach: five hundred errored samples become one class, the class becomes one question, and this is where an operator answers it — with a disposition, a required reason, and their name. One `ruling` event lands per class, sharing a proposal id when the decision answered one, which is what lets a group decision be unpicked later (workflow.md §5.6).

**It takes no claim**, for the same reason `ack` does not: a ruling is one append to an append-only file, and the moment that matters most is an operator reading a status while a tend is in flight.

**`--by` is free text naming an operator, with one role permitted.** An agent relaying an operator's decision records who decided, never itself — except for `dismiss`, the one disposition that marks nothing, which an agent may record as its own after investigating (`_anomaly.model.Ruling.by`). Every other answer changes what the results say and stays an operator's.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import click

from .._anomaly.model import (
    AGENT,
    Anomalies,
    Anomaly,
    AnomalyState,
    Disposition,
    agent_may,
    composed_effect,
)
from .._tend import status
from .._tend.items import finding_label
from .._tend.progress import display_keys
from .._workspace import RULING, append_event
from .anomalies import (
    listed,
    match_class,
    match_task,
    open_classes,
    persist_windows,
    precedent_lines,
    refuse_dishonest,
    settled_ruling,
)
from .turn import TURN_ERRORS, decided_by, find_workspace


@click.command("rule")
@click.argument("classes", nargs=-1, metavar="[FINDING|TASK]...")
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
    help="The answer. A finding a proposal covers takes the proposal's answer by default; given, this one overrides it. Required for a finding nothing proposes.",
)
@click.option(
    "--reason",
    required=True,
    help="Why. Recorded in the journal, attached as precedent to any recurrence, and the only account of the decision that survives.",
)
@click.option(
    "--by",
    default=None,
    help="Who decided — a name, never a role. Defaults to this workspace's git `user.name`, or the login name; pass it when relaying someone else's decision.",
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
    by: str | None,
    effect: str | None,
    output_json: bool,
) -> None:
    """Rule on anomaly classes: what the failures mean, and what happens to the data.

    Each argument names a finding — its label (`internet_egress`), `label:task` where the same finding is open on two tasks, its exception type (`TimeoutError`), or the class key as `steward status` prints it or any prefix of one — or a task by its display key (`cybench`, `cybench@openai`), which answers every finding proposed for that task as proposed. A finding a proposal covers is answered with the proposal's disposition unless `--disposition` says otherwise; one nothing proposes needs `--disposition`. A ruling closes the class's window — every open generation of it — and recurrence afterwards opens a new one carrying this decision as precedent.
    """
    workspace = find_workspace()
    decider = decided_by(workspace, by)
    try:
        result = status(workspace)
    except TURN_ERRORS as ex:
        raise click.ClickException(str(ex)) from ex
    anomalies = result.anomalies
    named = display_keys(result.progress)

    if proposal_id is not None:
        proposal_id = _live_proposal(anomalies, proposal_id)
        targets, decided = _from_proposal(anomalies, proposal_id, classes, disposition)
        decisions = {key: Decision(decided, proposal_id) for key in targets}
    else:
        if not classes:
            raise click.ClickException(
                "name at least one finding or task, or answer a proposal with "
                "--proposal ID"
            )
        targets = _targets(anomalies, named, classes)
        decisions = _decisions(anomalies, targets, disposition)

    # the doctrine and the effects are per disposition, and one answer can
    # now carry two -- a finding overridden beside its proposal's remainder
    effects: dict[str, str] = {}
    for decided in dict.fromkeys(decision.decided for decision in decisions.values()):
        keys = [
            key for key, decision in decisions.items() if decision.decided is decided
        ]
        _refuse_agent(decider, decided)
        refuse_dishonest(keys, decided)
        effects.update(
            _effects(anomalies, keys, decided, effect, result.dispositions.affected)
        )

    persist_windows(workspace.journal, result.anomaly_pending, targets)
    for key, decision in decisions.items():
        fields: dict[str, Any] = {
            "class": key,
            "disposition": decision.decided.value,
            "reason": reason,
            "by": decider,
            "effect": effects.get(key, ""),
        }
        if decision.proposal is not None:
            fields["proposal"] = decision.proposal
        append_event(workspace.journal, RULING, **fields)

    if output_json:
        click.echo(
            json.dumps(
                {
                    "ruled": [
                        {
                            "class": key,
                            "disposition": decision.decided.value,
                            "reason": reason,
                            "by": decider,
                            "effect": effects.get(key, ""),
                            "proposal": decision.proposal,
                        }
                        for key, decision in decisions.items()
                    ]
                },
                indent=2,
            )
        )
        return
    for key, decision in decisions.items():
        decided = decision.decided
        # `decider`, never `by` -- the option is `None` whenever the name
        # was resolved from the repository, and echoing it told an operator
        # their ruling was recorded `by None` while the journal beside it
        # correctly held their name. The `--json` branch above was already
        # right, which is the shape of every two-renderings bug here
        click.echo(
            f"ruled {finding_label(key)}: {decided.value} — {reason} "
            f"(by {decider}) · `{key}`"
        )
        if decision.proposal is not None:
            proposed = anomalies.proposals[decision.proposal].action
            how = (
                "as proposed" if proposed is decided else f"(proposed {proposed.value})"
            )
            click.echo(f"  answers {decision.proposal} {how}")
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


@dataclass(frozen=True)
class Decision:
    """What one class is ruled, and the proposal the ruling answers, if any."""

    decided: Disposition
    proposal: str | None = None


def _targets(
    anomalies: Anomalies, named: Mapping[str, str], tokens: tuple[str, ...]
) -> list[str]:
    """The classes the arguments name, a task token standing for every class proposed for it.

    Deduped, because two tokens naming one class must land one ruling, not a ruling immediately superseded by its own copy. A task fans out only to what is *proposed* for it — the operator is answering the question they were asked, and a class nobody has put to them is not part of it.
    """
    targets: list[str] = []
    for token in tokens:
        task = match_task(named, token)
        if task is None:
            keys = [_matched(anomalies, token)]
        else:
            keys = _proposed_for(anomalies, task)
            if not keys:
                raise click.ClickException(
                    f"nothing is proposed for {named[task]} — name a finding "
                    f"and give --disposition to rule one directly"
                )
        targets.extend(key for key in keys if key not in targets)
    return targets


def _proposed_for(anomalies: Anomalies, task: str) -> list[str]:
    """The classes proposed for one task — those whose every instance is in it."""
    return sorted(
        {
            anomaly.class_key
            for anomaly in anomalies.open
            if anomaly.state is AnomalyState.PROPOSED
            and anomaly.proposal is not None
            and anomaly.evidence.tasks
            and set(anomaly.evidence.tasks) <= {task}
        }
    )


def _decisions(
    anomalies: Anomalies, targets: list[str], disposition: str | None
) -> dict[str, Decision]:
    """Per class, the disposition it takes and the proposal that answers.

    A class a live proposal covers is answered through it — the ruling records the proposal, and takes its action unless `--disposition` overrides — so an agent answers by naming the finding, never by carrying the proposal's id from the question to the answer. A class nothing proposes needs the disposition said.
    """
    decisions: dict[str, Decision] = {}
    missing: list[str] = []
    for key in targets:
        window = anomalies.absorbing(key)
        covering = window.proposal if window is not None else None
        if disposition is not None:
            decisions[key] = Decision(Disposition(disposition), covering)
        elif covering is not None:
            decisions[key] = Decision(anomalies.proposals[covering].action, covering)
        else:
            missing.append(key)
    if missing:
        raise click.ClickException(
            f"--disposition is required — no proposal covers "
            f"{'this' if len(missing) == 1 else 'these'}, so nothing supplies "
            f"the answer:\n"
            + "\n".join(listed(missing))
            + "\none of "
            + ", ".join(entry.value for entry in Disposition)
        )
    return decisions


def _live_proposal(anomalies: Anomalies, token: str) -> str:
    """The live proposal an id or an unambiguous prefix names."""
    exact = [identifier for identifier in anomalies.proposals if identifier == token]
    matched = exact or [
        identifier for identifier in anomalies.proposals if identifier.startswith(token)
    ]
    if len(matched) > 1:
        raise click.ClickException(
            f"'{token}' matches {len(matched)} live proposals: "
            + ", ".join(sorted(matched))
        )
    if not matched:
        live = ", ".join(sorted(anomalies.proposals)) or "none are live"
        raise click.ClickException(
            f"no live proposal '{token}' — {live}. A proposal already "
            f"fully answered is no longer live; its classes rule directly"
        )
    return matched[0]


def _from_proposal(
    anomalies: Anomalies,
    proposal_id: str,
    classes: tuple[str, ...],
    disposition: str | None,
) -> tuple[list[str], Disposition]:
    """The classes a proposal answer covers, and the disposition it lands."""
    proposal = anomalies.proposals[proposal_id]
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
    shown = "\n".join(listed(keys)) if keys else "  (none are open)"
    raise click.ClickException(f"no open class matches '{token}' — open now:\n{shown}")


def _effects(
    anomalies: Anomalies,
    targets: list[str],
    decided: Disposition,
    effect: str | None,
    affected: Mapping[str, frozenset[str]],
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
    # the shared composition, so an operator's ruling and a policy's cannot word
    # the same mark differently
    return {key: composed_effect(anomalies, key, decided, affected) for key in targets}


def _open_windows(anomalies: Anomalies, key: str) -> list[Anomaly]:
    return [anomaly for anomaly in anomalies.open if anomaly.class_key == key]


def _refuse_agent(decider: str, decided: Disposition) -> None:
    """Refuse an agent recording a decision that is not its to make.

    **The one role `--by` accepts, and only for `dismiss`.** An agent that has investigated a class and found no case to answer is reporting an absence, and requiring a signature for it cost one human decision per false positive while protecting nothing. Marking the data is the opposite: `accept` attaches a caveat the report carries, and `exclude`, `zero` and `score` change what the numbers are computed over. A run certified because a machine ran out of things to flag is the failure the whole verb exists to prevent, and this is the line that keeps `--by agent` on the harmless side of it.

    Args:
        decider: What `--by` resolved to.
        decided: The disposition being recorded.

    Raises:
        click.ClickException: Where an agent named itself for anything but `dismiss`.
    """
    if decider.strip().lower() != AGENT or agent_may(decided):
        return
    raise click.ClickException(
        f"{decided.value} marks the data, so it is an operator's decision — "
        f"an agent may record only dismiss as its own. Propose it instead "
        f"(`steward propose`) and record the answer with their name."
    )
