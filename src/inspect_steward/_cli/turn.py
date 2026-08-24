"""What `tend` and `status` print, and the errors both of them convert.

One renderer for two verbs, differing only in tense: a tend reports what it did, a status reports what a tend would do. Keeping them in one function is the same argument that keeps the two verbs one code path — a preview that describes something other than what happens next is worse than no preview.
"""

import dataclasses
import json

import click

from .._evalset.manifest import ManifestError
from .._evalset.observe import TaskState
from .._schedule import ManifestVersionError
from .._tend import Refused, TendError, TendResult
from .._workspace import DirectivesError, Held, Workspace

TURN_ERRORS = (TendError, ManifestError, ManifestVersionError, DirectivesError)
"""Everything a turn raises that is a message for a person rather than a traceback."""


def find_workspace() -> Workspace:
    """The workspace containing the current directory.

    Raises:
        click.ClickException: If there is none.
    """
    if (workspace := Workspace.find()) is None:
        raise click.ClickException(
            "no Steward workspace here (or in any parent directory) — run "
            "`steward init` to create one"
        )
    return workspace


def echo_refused(refused: Refused) -> None:
    """Report a claim somebody else holds.

    Not an error, and it should not read as one: a timer firing while an agent is mid-tend is the ordinary case, and the right response is to do nothing, because the work is already being done.
    """
    held = refused.held
    since = f" since {held.since}" if held.since else ""
    who = f"pid {held.pid}" if held.pid else "another process"
    click.echo(f"a {held.command or 'command'} has been running{since} ({who}).")
    if held.unbroken:
        click.echo(f"it looks wedged, and could not be cleared: {held.unbroken}")
    else:
        click.echo("nothing to do — it holds the claim and is doing this turn's work.")


def echo_turn(result: TendResult) -> None:
    """Print a turn: where the run stands, then what it did or would do."""
    summary = result.summary
    states = ", ".join(
        f"{summary.states.get(state.value, 0)} {state.value}"
        for state in TaskState
        if summary.states.get(state.value, 0)
    )
    click.echo(f"{summary.tasks} tasks: {states or 'none'}")

    if result.executed:
        did = _counts(
            (len(result.spawned), "spawned"),
            (len(result.reaped), "reaped"),
            (len(result.archived), "archived"),
        )
        click.echo(f"{summary.running} running · {did or 'nothing to do'}")
    else:
        would = _counts(
            (summary.spawning, "to spawn"),
            (summary.archiving, "to archive"),
        )
        click.echo(f"{summary.running} running · next tend: {would or 'nothing to do'}")
    if summary.queued:
        click.echo(
            f"{summary.queued} waiting on a slot (ceiling {summary.max_workers})"
        )

    for line in _attention(result):
        click.echo(line)


def _counts(*pairs: tuple[int, str]) -> str:
    return ", ".join(f"{count} {label}" for count, label in pairs if count)


def _attention(result: TendResult) -> list[str]:
    """The lines worth interrupting someone with, if any."""
    summary = result.summary
    lines: list[str] = []

    if summary.stalled:
        lines.append(
            f"! {len(summary.stalled)} "
            f"{'task has' if len(summary.stalled) == 1 else 'tasks have'} stopped "
            f"making progress and will not be respawned"
        )
    if summary.orphans_running:
        lines.append(
            f"! {len(summary.orphans_running)} running "
            f"{'worker is' if len(summary.orphans_running) == 1 else 'workers are'} "
            f"running work the definition no longer asks for"
        )
    if result.drift:
        lines.append(
            "! the definition has changed since it was captured — "
            "run `steward launch` to apply it"
        )
    if result.degraded is not None:
        lines.append(f"! _steward.md could not be read: {result.degraded}")
        lines.append("  running on the settings the last turn recorded")
    if summary.unreadable:
        lines.append(
            f"! {summary.unreadable} "
            f"{'file' if summary.unreadable == 1 else 'files'} in the log "
            f"directory could not be read as logs"
        )
    for failure in result.failures:
        lines.append(f"! {failure}")
    if (broke := result.broke) is not None:
        lines.append(f"! cleared a wedged claim held by pid {broke.pid}")
    if (claim := result.claim) is not None:
        lines.append(_claim_line(claim))

    return lines


def _claim_line(claim: Held) -> str:
    since = f" since {claim.since}" if claim.since else ""
    return f"  a {claim.command or 'command'} holds the claim{since}"


def turn_json(result: TendResult) -> str:
    """A turn as JSON, for an agent rather than a person."""
    return json.dumps(dataclasses.asdict(result), indent=2, default=str)


def refused_json(refused: Refused) -> str:
    """A refusal as JSON, shaped so a caller can branch on one field."""
    return json.dumps(
        {"refused": True, "held": dataclasses.asdict(refused.held)},
        indent=2,
        default=str,
    )
