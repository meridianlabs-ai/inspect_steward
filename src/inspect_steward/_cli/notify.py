"""`steward notify` — the agent saying something no trigger could have said.

Steward's own posts are arithmetic: an item appeared, a task finished, the queue emptied. They are worth having and they are the smaller half. The valuable half is an agent that has read forty logs at 2am, found the thing none of those triggers describes, and can put one sentence in front of a person (workflow.md §11.1). That is what this verb is.

**Two kinds, and the other four are refused rather than merely undocumented.** `attention` and `stopped` carry judgement, which is what makes them the agent's to send. A hand-sent `gate` is a claim about the run that nobody computed; a hand-sent `signed_off` is a claim that a human adjudicated, which is the entire content of signoff. Both are the kind of thing that reads as authoritative in a channel precisely because it is usually true.

**It takes no claim and writes no journal entry.** Sending a message changes nothing about the run, and the case that matters most is an agent posting while a tend is in flight. What it says is in the collection the agent already made and in whatever it does next.
"""

import click

from .._notify import (
    AGENT_KINDS,
    GLYPH,
    Kind,
    Post,
    channel_apprise,
    establish_channel,
    send_post,
)
from .._workspace import DirectivesError, read_directives
from .turn import find_workspace

KINDS = sorted(kind.value for kind in AGENT_KINDS)
"""What may be sent from here, in the order `--help` lists them."""


@click.command("notify")
@click.argument("message")
@click.option(
    "--kind",
    type=click.Choice(KINDS),
    default=Kind.ATTENTION.value,
    show_default=True,
    help=(
        "Why you are sending this. `attention` is worth knowing and work "
        "continues; `stopped` means nothing progresses until a person answers."
    ),
)
@click.option(
    "--detail",
    multiple=True,
    metavar="TEXT",
    help=(
        "A supporting line, under the message. Repeatable — one per thing you "
        "want the reader to see without opening anything."
    ),
)
def notify_command(message: str, kind: str, detail: tuple[str, ...]) -> None:
    """Post MESSAGE to this run's notification channel.

    MESSAGE is the title — the line that stands alone in a phone notification, so make it the thing you would want read if nothing else was. Everything else goes in --detail.

    The channel is the run's own: `notification` in _steward.yaml, STEWARD_NOTIFICATION, or INSPECT_EVAL_NOTIFICATION, whichever is set.
    """
    workspace = find_workspace()
    try:
        directives = read_directives(workspace.directives)
    except DirectivesError:
        # a file that will not parse is not a reason to refuse to speak -- it is
        # one of the things most worth saying. `establish_channel` reads the
        # variable on its own when there is no `Directives` to ask
        directives = None

    if establish_channel(workspace, directives) is None:
        raise click.ClickException(
            "this run has no notification channel — set `notification` in "
            "_steward.yaml, or export STEWARD_NOTIFICATION, to an Apprise URL"
        )
    if (instance := channel_apprise()) is None:
        raise click.ClickException(
            "the notification channel is configured but could not be built — "
            "check that the URL or config file Apprise was given is usable"
        )

    # the glyph Steward's own posts lead with, because a reader scanning a
    # channel sorts on the first character and an agent saying a run is stopped
    # is the same news as the run being stopped
    post = Post(
        kind=Kind(kind),
        workspace=workspace.root.name,
        glyph=GLYPH[Kind(kind)],
        title=message,
        lines=list(detail),
    )
    # **`send_post` reports rather than raises**, because its ordinary caller is
    # a turn that must not fail. Here the caller *is* the message, so a failure
    # to deliver has to be the command's exit status -- and every target counts,
    # not merely one of them: a turn's post is owed to a reader and one channel
    # reaching them is enough, but an agent that asked for this one is owed the
    # truth about what it asked for
    if failures := send_post(instance, post, workspace.log).failures:
        raise click.ClickException("\n".join(failures))
    click.echo(f"sent ({kind})")


__all__ = ["KINDS", "notify_command"]
