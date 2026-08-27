"""Naming an item on a command line.

Two commands take an item id — `steward ack` disposes of one, `steward raise` hands one to its owner — and both take any unambiguous prefix, because a full id is something a person has to type. The matching is here so the two cannot come to disagree about what a prefix means, and the *messages* are not, because the three ways a match can fail say different things depending on which act was attempted.
"""

import click

from .._tend.items import Item


def match_item(items: list[Item], token: str) -> Item | None:
    """The one item this token names, or `None` where it names none.

    An exact id wins outright before prefixes are considered, so an id that happens to be a prefix of a longer one is still nameable — which `stalled:t:2` and `stalled:t:20` make a real case rather than a theoretical one.

    Args:
        items: The items a token may name — already filtered to those the caller's act applies to.
        token: A full id or a prefix of one.

    Returns:
        The match, or `None` for no match at all. The caller composes that message, since *no such item* and *already dealt with* are the same empty list and not the same mistake.

    Raises:
        click.ClickException: If the token names more than one, listing them. Guessing between two items somebody is about to record a decision against is the one outcome worth refusing.
    """
    exact = [entry for entry in items if entry.id == token]
    matched = exact or [entry for entry in items if entry.id.startswith(token)]
    if len(matched) > 1:
        raise click.ClickException(
            f"'{token}' matches {len(matched)} items:\n"
            + "\n".join(f"  {entry.id}" for entry in matched)
        )
    return matched[0] if matched else None
