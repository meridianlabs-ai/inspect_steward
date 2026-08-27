"""What `tend` and `status` print, and the errors both of them convert.

One renderer for two verbs, differing only in tense: a tend reports what it did, a status reports what a tend would do. Keeping them in one function is the same argument that keeps the two verbs one code path — a preview that describes something other than what happens next is worse than no preview.
"""

import dataclasses
import json
import shutil

import click

from .._evalset.cost import fleet_width, projection
from .._evalset.manifest import ManifestError
from .._evalset.observe import TaskState
from .._schedule import Blocked, ManifestVersionError, Summary
from .._tend import (
    HEADINGS,
    Refused,
    TendError,
    TendResult,
    by_owner,
    progress_table,
    verdict_line,
)
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


def echo_turn(result: TendResult, *, table: bool = True) -> None:
    """Print a turn: where the run stands, then what it did or would do."""
    summary = result.summary
    click.echo(verdict_line(result.verdict, result.items))
    states = ", ".join(
        f"{summary.states.get(state.value, 0)} {state.value}"
        for state in TaskState
        if summary.states.get(state.value, 0)
    )
    click.echo(f"{summary.tasks} tasks: {states or 'none'}")

    if table:
        for line in progress_table(result.progress, width=_key_width()):
            click.echo(line)

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
        # a paused run queues everything, and saying it waits on a slot would
        # name the limit as the reason when the reason is the pause -- which
        # is the one thing a reader might go and change. Otherwise the limit
        # named is the one `reconcile` found binding rather than the one that
        # happens to be set: a run short of processes but well under its task
        # ceiling is waiting on `max_workers`, and sending its operator to
        # `max_tasks` would send them to a number that changes nothing
        if summary.paused:
            waiting = "waiting on a resume"
        elif summary.blocked is Blocked.MAX_WORKERS:
            waiting = f"waiting on a worker (max_workers {summary.max_workers})"
        elif summary.blocked is Blocked.MAX_TASKS:
            waiting = f"waiting on a slot (max_tasks {summary.max_tasks})"
        else:
            waiting = "waiting"
        click.echo(f"{summary.queued} {waiting}")

    # what the run's width can cost to start, from the capture that read the
    # definition. A ceiling rather than an estimate, since capture builds every
    # task and a worker builds its own. Silent when nothing measured it, which
    # is every manifest committed before the measurement existed. The only
    # place this is printed -- `launch` echoes a turn like every other verb
    if (line := _cost_line(summary)) is not None:
        click.echo(line)

    for line in _attention(result):
        click.echo(line)


def _cost_line(summary: Summary) -> str | None:
    """The startup-memory bound for this run's shape, or `None` if unmeasured."""
    return projection(
        summary.capture_rss,
        fleet_width(
            summary.tasks,
            max_workers=summary.max_workers,
            max_tasks=summary.max_tasks,
        ),
    )


def _key_width() -> int:
    """How much of a display key the terminal can spare.

    The numeric columns are what a reader scans, so they get the room: a sweep
    key with three arguments and a model runs past eighty characters and would
    push the counts off the edge. Half the terminal, floored at something a
    task name still survives.
    """
    return max(28, shutil.get_terminal_size(fallback=(100, 24)).columns // 2)


def _counts(*pairs: tuple[int, str]) -> str:
    return ", ".join(f"{count} {label}" for count, label in pairs if count)


def _attention(result: TendResult) -> list[str]:
    """The lines worth interrupting someone with, if any.

    The items, grouped by who resolves them, each with the id `steward ack` takes. Then the two machinery notes that are not items — a claim somebody else holds and a wedged one this turn cleared are facts about Steward rather than conditions anyone has to dispose of.
    """
    lines: list[str] = []
    for owner, group in by_owner(result.items):
        lines.append(f"{HEADINGS[owner]}:")
        for item in group:
            lines.append(f"  ! {item.summary}")
            trailer = item.id if item.acknowledgeable else "(transient)"
            if item.action is not None:
                trailer = f"{trailer} · {item.action}"
            lines.append(f"    {trailer}")

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
