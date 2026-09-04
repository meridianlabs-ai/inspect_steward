"""Naming an anomaly class on a command line.

Three verbs take class keys — `rule` decides them, `propose` groups them, `investigate` holds one — and all three match the same way `ack` matches items: an exact key wins outright, then any unambiguous prefix, and guessing between two classes somebody is about to record a decision against is the one outcome worth refusing.

The failure messages are composed per verb, because the three ways a token can fail to name a class say different things depending on the act attempted — except the two shared here, which mean the same thing everywhere: *ambiguous* and *already settled*.

The disposition matrix is shared too, because `propose` must not put in front of an operator a question `rule` would refuse to answer. And so is `persist_windows`, the step every deciding verb takes first: a `status` computes newly detected windows without writing them, so the fold a verb consulted can be ahead of the journal — and a decision recorded against a window the journal does not hold would be skipped by the next fold, reported successful, and ignored.
"""

from collections.abc import Sequence
from pathlib import Path

import click

from .._anomaly.fold import Pending
from .._anomaly.model import (
    SAMPLE_MARKS,
    Anomalies,
    Anomaly,
    Disposition,
    Ruling,
    honest,
)
from .._evalset.classify import kind_of
from .._workspace import INSTANCE, OPENED, append_event

DOCTRINE = (
    "an errored sample has exactly four honest answers — rerun, exclude, zero, "
    "or score (workflow.md §12)"
)


def refuse_dishonest(targets: list[str], decided: Disposition) -> None:
    """The disposition-kind pairings refused rather than recorded.

    The matrix itself is `_anomaly.model.honest` — shared with the tend's policy rulings, so a pattern cannot grant what an operator could not type. Three rows. `accept` on an `error:` class would leave errored samples in the data with a caveat saying so — silent exclusion wearing a decision's clothes, precisely what the four answers exist to prevent. The three sample marks (`exclude`, `zero`, `score`) mean nothing where the residue is not a sample's data: a `task:` or `score:` class has no sample population to mark at all, and a `scanerror:` class has one and still nothing to mark, since what it left behind is a missing verdict rather than a wrong row. And `rerun` on a `scanerror:` class names an act nothing can carry out — the eval is fine and only the reading of it failed, so there are no samples to requeue.

    Raises:
        click.ClickException: Naming each refused class and its kind.
    """
    refused = [key for key in targets if not honest(kind_of(key), decided)]
    if not refused:
        return
    if decided is Disposition.ACCEPT:
        raise click.ClickException(
            f"accept is refused for {', '.join(refused)} — {DOCTRINE}"
        )
    kinds = ", ".join(f"{key} ({kind_of(key)} class)" for key in dict.fromkeys(refused))
    if decided is Disposition.RERUN:
        raise click.ClickException(
            f"rerun re-runs samples, and the samples behind {kinds} are fine — "
            f"only the scan of them failed, and Steward has no verb that "
            f"re-scans — accept (with --effect) and dismiss are the answers "
            f"that fit"
        )
    raise click.ClickException(
        f"{decided.value} marks a sample's data, and there is nothing to mark "
        f"behind {kinds} — rerun, accept (with --effect), "
        f"and dismiss are the answers that fit"
    )


def persist_windows(
    journal: Path, pending: Sequence[Pending], targets: Sequence[str]
) -> None:
    """Put the windows a decision applies to on the record, before the decision.

    Appends the target classes' pending `opened`/`instance` events — the ones a `status` folded in memory and did not write. Safe beside a concurrent tend appending the same events: an `opened` with an absorbing window standing is skipped by the fold, and an `instance` whose refs are all absorbed changes nothing.
    """
    for entry in pending:
        if entry.type in (OPENED, INSTANCE) and entry.fields.get("class") in targets:
            append_event(journal, entry.type, **entry.fields)


def open_classes(anomalies: Anomalies) -> list[str]:
    """The class keys a verb can act on, sorted — every non-terminal window's."""
    return sorted({anomaly.class_key for anomaly in anomalies.open})


def match_class(keys: list[str], token: str) -> str | None:
    """The one open class this token names, or `None` where it names none.

    Args:
        keys: The class keys the caller's act applies to.
        token: A full class key or a prefix of one.

    Returns:
        The match, or `None` — the caller composes that message, since *no such class* and *already ruled* are the same empty result and not the same mistake.

    Raises:
        click.ClickException: If the token names more than one, listing them.
    """
    exact = [key for key in keys if key == token]
    matched = exact or [key for key in keys if key.startswith(token)]
    if len(matched) > 1:
        raise click.ClickException(
            f"'{token}' matches {len(matched)} classes:\n"
            + "\n".join(f"  {key}" for key in matched)
        )
    return matched[0] if matched else None


def settled_ruling(anomalies: Anomalies, token: str) -> Ruling | None:
    """The most recent ruling on a settled class this token names, if that is what it names.

    What turns *no open class matches* into the honest message: the class existed, somebody already decided it, and recurrence — not re-ruling — is what would open it again.
    """
    matched = [
        anomaly
        for anomaly in anomalies.settled
        if (anomaly.class_key == token or anomaly.class_key.startswith(token))
        and anomaly.ruling is not None
    ]
    if not matched:
        return None
    return max(matched, key=lambda anomaly: anomaly.generation).ruling


def precedent_lines(anomaly: Anomaly) -> list[str]:
    """Prior rulings on this class, as the short lines a decision is shown beside."""
    return [
        f"{ruling.disposition.value} by {ruling.by} at {ruling.ts}: {ruling.reason}"
        for ruling in anomaly.precedent
    ]


__all__ = [
    "DOCTRINE",
    "SAMPLE_MARKS",
    "match_class",
    "open_classes",
    "persist_windows",
    "precedent_lines",
    "refuse_dishonest",
    "settled_ruling",
]
