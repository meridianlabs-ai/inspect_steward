"""`steward signoff` — the one command whose *decision* is never the agent's.

Running it can be. An agent that notices a run is ready and tells the person is doing its job, and carrying out their answer is the same act as `steward rule --by` relaying a ruling: what goes in `--by` is the name of whoever decided, so a signature stays traceable to a person whichever process typed it. What would make the record meaningless is not an agent at the keyboard but a signature nobody asked for.

The refusal is the interesting half. It prints every blocker rather than the first, because a person who fixes one and meets another has walked exactly the loop the gate exists to collapse — the same discipline `launch`'s archive gate already keeps. And each blocker names the act that answers it, because what is being refused is never a hole: it is an *unnamed* hole, and the answer is to name it with a ruling rather than to make it go away.
"""

import json
import shutil
import textwrap
from pathlib import Path
from typing import NoReturn

import click

from .._evalset.manifest import ManifestError
from .._signoff import Signoff, SignoffError, committed_manifest, signoff
from .._workspace import Held
from .turn import TURN_ERRORS, find_workspace


@click.command("signoff")
@click.option(
    "--by",
    required=True,
    help="Who is accepting these results — a name, never a role. An agent relaying a person's decision records the person.",
)
@click.option(
    "--note",
    default=None,
    help="What you want said about the acceptance. Optional: the account of every decision is already in the journal.",
)
@click.option(
    "--again",
    is_flag=True,
    default=False,
    help="Record a second signature over a run whose first one still stands.",
)
@click.option(
    "--publish",
    is_flag=True,
    default=False,
    help="Put the signed logs into the configured log store, so another project can reuse them instead of running the task. Never happens without this flag — there is no setting that turns it on.",
)
@click.option(
    "--no-break-claim",
    is_flag=True,
    default=False,
    help="Refuse if another command is wedged, rather than killing it and taking the claim.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the signature, or the blockers, as JSON.",
)
def signoff_command(
    by: str,
    note: str | None,
    again: bool,
    publish: bool,
    no_break_claim: bool,
    output_json: bool,
) -> None:
    """Attest that these results are accepted, and end the run.

    Runs a final turn, refuses with every blocker at once if anything is still unnamed, moves superseded attempts into `logs-archive/`, records who signed and what they signed over, and takes the timer down. It does not commit the journal — that stays yours.

    A person decides this. An agent may prompt for it and may run it once they answer, recording their name, which is why the signer is recorded rather than the process. `--publish` is the same shape one step further out: exporting results into a shared store is the person's call too, so it is asked rather than configured.
    """
    workspace = find_workspace()
    try:
        committed_manifest(workspace)
        result = signoff(
            workspace,
            by=by,
            note=note,
            again=again,
            publish=publish,
            break_stale=not no_break_claim,
        )
    except (SignoffError, ManifestError, *TURN_ERRORS) as ex:
        raise click.ClickException(str(ex)) from ex

    if isinstance(result, Held):
        _echo_held(result)

    if output_json:
        click.echo(_signoff_json(result))
    else:
        _echo_signoff(result, workspace.root)
    if result.signature is None:
        # after the detail rather than instead of it, and an error rather than a
        # note: a refusal that exits zero is a refusal a script does not notice
        raise click.ClickException(_refusal(result))
    if result.unverified is not None:
        # **a signature that could not be checked exits the same way a refusal
        # does**, and for the same reason: the run is not finished, the timer
        # is deliberately still up, and a script told otherwise would move on
        raise click.ClickException(
            f"the signature is recorded, but this run is not finished — "
            f"{result.unverified}"
        )


def _refusal(result: Signoff) -> str:
    """Why the gate said no, as one closing sentence under the blockers already printed."""
    count = len(result.blockers)
    return (
        f"nothing was signed — {count} thing{'s' if count != 1 else ''} "
        f"{'are' if count != 1 else 'is'} still open above"
    )


def _wrapped(text: str) -> list[str]:
    """One remedy, broken to the terminal's width under a four-space indent."""
    width = max(48, shutil.get_terminal_size(fallback=(100, 24)).columns - 4)
    return textwrap.wrap(text, width=width, break_long_words=False) or [text]


def _echo_signoff(result: Signoff, root: Path) -> None:
    """Print a signoff: what stopped it, or what it recorded."""
    for warning in result.warnings:
        click.echo(f"! {warning}")
    if result.signature is None:
        click.echo("this run cannot be signed yet:")
        for blocker in result.blockers:
            click.echo(f"  - {blocker.summary}")
            # wrapped, unlike everything else this CLI prints, because a remedy
            # is the one string here that is a *sentence with options in it*
            # rather than a count or a command — and a reader who has to
            # untangle a soft-wrapped line to find the second option will take
            # the first one
            for line in _wrapped(blocker.remedy):
                click.echo(f"    {line}")
        return

    signature = result.signature
    click.echo(f"🔒 signed off by {signature.by} at {signature.ts}")
    if signature.note:
        click.echo(f"  {signature.note}")
    if signature.exceptions:
        count = len(signature.exceptions)
        click.echo(f"  {count} accepted exception{'s' if count != 1 else ''}:")
        for key in signature.exceptions:
            click.echo(f"    {key}")
    else:
        click.echo("  no accepted exceptions")
    if result.curated is not None and result.curated.moved:
        moved = len(result.curated.moved)
        click.echo(
            f"  archived {moved} superseded attempt{'s' if moved != 1 else ''} — "
            f"logs/ now holds what was signed"
        )
    if result.published is not None:
        published = result.published
        act = "indexed" if published.kind == "indexed" else "copied"
        click.echo(
            f"  {act} {published.count} log{'s' if published.count != 1 else ''} "
            f"into the log store — other projects can reuse this work"
        )
    if result.unpublished is not None:
        click.echo(f"  {result.unpublished}")
    if result.disarmed is not None:
        click.echo(f"  disarmed {result.disarmed} — nothing tends this run now")
    if result.unverified is not None:
        click.echo(f"  ! {result.unverified}")
        return
    # **the last word, because it is the one thing signoff does not do.** The
    # journal is the record this attestation lives in, and committing it stays
    # the human's job (workflow.md §18 q4); what the verb owes is that the
    # record is complete and quiescent by the time it says so.
    #
    # The workspace is named only when the reader is not standing in it — a
    # resolved path is frequently a hundred characters, and printing one on
    # the line that ends the run buries the sentence under it
    where = "" if root == Path.cwd() else f" in {root.name}"
    click.echo(
        f"  nothing further will be written — the journal{where} is yours to commit"
    )


def _echo_held(held: Held) -> NoReturn:
    """Report a claim somebody else holds, and stop.

    An error rather than an outcome, for the reason `launch`'s is: a tend that cannot run is a turn skipped and the next one covers it, where a signoff that cannot run means the attestation somebody just made did not happen.
    """
    since = f" since {held.since}" if held.since else ""
    who = f"pid {held.pid}" if held.pid else "another process"
    click.echo(f"a {held.command or 'command'} holds the claim{since} ({who}).")
    if held.unbroken:
        click.echo(f"it looks wedged, and could not be cleared: {held.unbroken}")
    raise click.ClickException("nothing was signed — try again when it is done.")


def _signoff_json(result: Signoff) -> str:
    """A signoff as JSON, shaped so a caller can branch on one field."""
    signature = result.signature
    return json.dumps(
        {
            "signed": signature is not None,
            "signature": None
            if signature is None
            else {
                "by": signature.by,
                "note": signature.note,
                "ts": signature.ts,
                "digest": signature.digest,
                "exceptions": list(signature.exceptions),
            },
            "blockers": [
                {
                    "kind": blocker.kind,
                    "summary": blocker.summary,
                    "remedy": blocker.remedy,
                }
                for blocker in result.blockers
            ],
            "curated": []
            if result.curated is None
            else [destination for _, destination in result.curated.moved],
            "published": None
            if result.published is None
            else {
                "kind": result.published.kind,
                "logs": result.published.count,
                # a partial publication is a real outcome, and the count alone
                # cannot say whether it was one: a caller reading this has to
                # be able to tell nine of nine from nine of ten
                "failed": list(result.published.failed),
            },
            "unpublished": result.unpublished,
            "disarmed": result.disarmed,
            "unverified": result.unverified,
            "warnings": result.warnings,
        },
        indent=2,
    )
