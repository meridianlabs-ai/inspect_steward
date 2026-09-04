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
    Owner,
    Refused,
    TendError,
    TendResult,
    anomalies_line,
    by_owner,
    progress_table,
    status_headline,
)
from .._tend.anomalies_md import OUTCOMES_HEADER, outcomes_cells
from .._tend.table import RESOURCES_HEADER, plain_table, resources_cells
from .._workspace import DirectivesError, Held, Workspace, operator_name

TURN_ERRORS = (TendError, ManifestError, ManifestVersionError, DirectivesError)
"""Everything a turn raises that is a message for an operator rather than a traceback."""


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


def decided_by(workspace: Workspace, by: str | None) -> str:
    """The operator a decision is recorded against.

    `--by` when given, passed through untouched so the verb's own refusal of a blank name still fires; otherwise the workspace's git `user.name`, or the login name. The default exists so that an agent relaying the operator whose shell this is need not ask them their name — and it is refused rather than guessed when neither source answers, because a decision recorded against nobody is worse than one that asked.

    Raises:
        click.ClickException: If `--by` was omitted and no name can be resolved.
    """
    if by is not None:
        return by
    name = operator_name(workspace.root)
    if not name:
        raise click.ClickException(
            "could not work out who is deciding — pass --by NAME"
        )
    return name


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
    # the operator's shape, in step with `status.md` (render.py): this is what
    # an operator at the terminal reads, and the agent reads `collect`
    click.echo(status_headline(result))

    # decisions before the run, because surfacing what an operator has to decide is
    # what this output is mainly for and everything else is context for it. It
    # used to come last, under the task table, and the M2 gate showed the cost:
    # the verdict said something needed an operator and finding out what meant
    # scrolling past every task that did not (agent.md §4.1)
    for line in _attention(result):
        click.echo(line)

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
    for line in _anomalies(result):
        click.echo(line)

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

    for line in _live(result):
        click.echo(line)

    for index, line in enumerate(result.tuning.lines):
        # one source with the markdown block (`TuningPlan.lines`), so the two
        # renderings cannot disagree about what the window supported
        click.echo(f"tuning: {line}" if index == 0 else f"  {line}")

    for index, rule in enumerate(result.policies):
        # said here because `_steward.yaml` is no longer the only place they can
        # come from: an agent told to read the file would be reading half of
        # them on a machine that exports STEWARD_POLICIES
        click.echo(f"standing rules: {rule}" if index == 0 else f"  {rule}")

    # one source with the markdown's own line (`render._notification`), so the
    # two renderings cannot disagree about whether anything reaches an operator
    if result.notification is not None:
        click.echo(f"notifications: {result.notification.description}")

    for line in _machinery(result):
        click.echo(line)


def _anomalies(result: TendResult) -> list[str]:
    """Where the anomaly queue stands, then by task the samples that did not take the normal course.

    The line is `render.anomalies_line`, one source with the agent's page; the table is the one `status.md` carries, drawn without pipes. Under one label, since a reader who sees *anomalies: 5 open* and a table both named anomalies has been told one thing twice.
    """
    line = anomalies_line(result.anomalies)
    cells = outcomes_cells(
        result.dispositions.outcomes, result.progress, width=_key_width()
    )
    if line is None and not cells:
        return []
    lines = [line or "anomalies:"]
    if cells:
        lines += plain_table(OUTCOMES_HEADER, cells, indent="  ")
    return lines


def _live(result: TendResult) -> list[str]:
    """What the running processes cost, or — before any are running — what starting them would.

    One or the other, in step with the markdown's own block: while something runs the per-task table is the answer, and before anything does the capture's ceiling is (agent.md §4.2). The caveat travels with the figures because every one of them shrinks as a run completes, and a falling refusal count otherwise reads as a problem fixing itself.
    """
    if result.progress.live is not None:
        cells = resources_cells(result.progress, width=_key_width())
        if not cells:
            return []
        return ["resources:", *plain_table(RESOURCES_HEADER, cells, indent="  ")]

    # what the run's width can cost to start, from the capture that read the
    # definition. A ceiling rather than an estimate, since capture builds every
    # task and a worker builds its own. Silent when nothing measured it, which
    # is every manifest committed before the measurement existed. The only
    # place this is printed -- `launch` echoes a turn like every other verb
    bound = _cost_line(result.summary)
    return [bound] if bound is not None else []


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
    """What is waiting on the operator, one summary per line.

    Their items alone and the summary alone, as `status.md` has them: the agent's queue is a count in the headline, and the ids are on the agent's page (`steward collect`), which is where the verbs that take them are run from. A raised item is still shown — it is still open and an operator still owes an answer; what raising records is that the agent's part is done (agent.md §2.2).
    """
    lines: list[str] = []
    for owner, group in by_owner(result.items):
        if owner is not Owner.OPERATOR:
            continue
        lines.append(f"{HEADINGS[owner]}:")
        lines.extend(f"  ! {item.summary}" for item in group)
    return lines


def _machinery(result: TendResult) -> list[str]:
    """The two notes that are not items, and stay at the bottom because of it.

    A claim somebody else holds and a wedged one this turn cleared are facts about *Steward* rather than conditions anyone has to dispose of — so they do not belong in the decisions block, which is now the first thing a reader meets and has to stay answerable.
    """
    lines: list[str] = []
    if (broke := result.broke) is not None:
        lines.append(f"! cleared a wedged claim held by pid {broke.pid}")
    if (claim := result.claim) is not None:
        lines.append(_claim_line(claim))
    return lines


def _claim_line(claim: Held) -> str:
    since = f" since {claim.since}" if claim.since else ""
    return f"  a {claim.command or 'command'} holds the claim{since}"


def turn_json(result: TendResult) -> str:
    """A turn as JSON, for an agent rather than an operator."""
    return json.dumps(dataclasses.asdict(result), indent=2, default=str)


def refused_json(refused: Refused) -> str:
    """A refusal as JSON, shaped so a caller can branch on one field."""
    return json.dumps(
        {"refused": True, "held": dataclasses.asdict(refused.held)},
        indent=2,
        default=str,
    )
