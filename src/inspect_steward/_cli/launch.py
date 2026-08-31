"""`steward launch` — starting a run, and the one moment a person is present.

Almost every other surface in Steward is written for a reader who is not there: `status.md` for whoever arrives in the morning, the journal for an agent with no memory of last night, the item list for a channel. This one is written for somebody at a terminal who is about to walk away, which changes what the output is for. It has to say what the run *is* — which backend is watching it, how many tasks, how many samples — and it has to put the one thing that could be a mistake where it cannot be missed.

**The delta is printed before the outcome, and printed even when the launch is refused.** A refusal whose reason is invisible teaches people to reach for `--accept-archive` reflexively, which is the same as not having a gate.
"""

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, NoReturn

import click

from .._evalset.detect import DefinitionType
from .._launch import Change, Delta, Launch, LaunchError, launch
from .._notify import INSPECT_NOTIFICATION, usable_channel
from .._util.duration import format_duration
from .._workspace import DirectivesError, Held, Workspace, read_directives
from .options import (
    PassthroughCommand,
    Setting,
    collect_overrides,
    overrides,
    passthrough_options,
    shape_options,
    sync_options,
    tend_interval_option,
)
from .tasks import parse_args
from .turn import TURN_ERRORS, echo_turn, find_workspace

_LABELS = {
    Change.ADD: ("add", ""),
    Change.EXTEND: ("extend", ""),
    Change.RESTORE: ("restore", "already run by this project"),
    Change.REMOVED: ("archive", "removed from the definition"),
    Change.SUPERSEDED: ("archive", "args changed → superseded"),
}
"""Verb and explanation per row. The archive rows share a verb and differ in their reason, because *what it does* and *why* are separately useful: the verb is what the gate is about, and the reason is what tells somebody whether they meant it."""


@click.command("launch", cls=PassthroughCommand)
@click.argument(
    "definition",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=False,
)
@click.option(
    "--arg",
    "-A",
    "definition_args",
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "Argument for the definition (flow spec function args only). Can be "
        "specified multiple times. Defaults to the committed manifest's on a "
        "re-launch."
    ),
)
@click.option(
    "--no-args",
    "no_args",
    is_flag=True,
    default=False,
    help=(
        "Capture with no definition arguments, rather than reusing the "
        "committed manifest's."
    ),
)
@click.option(
    "--no-overrides",
    "no_overrides",
    is_flag=True,
    default=False,
    help=(
        "Capture at the definition's own shape, rather than reusing the "
        "overrides the committed manifest recorded. Ignores STEWARD_* and "
        "INSPECT_EVAL_* for this launch too."
    ),
)
@click.option(
    "--type",
    "definition_type",
    type=click.Choice(["evalset", "flow", "hawk"]),
    default=None,
    help="Definition type (auto-detected, or taken from the committed manifest).",
)
@click.option(
    "--accept-archive",
    is_flag=True,
    default=False,
    help=(
        "Commit even though results would leave logs/ — archived, or left "
        "behind by a log directory that moved."
    ),
)
@click.option(
    "--no-timer",
    "timer",
    is_flag=True,
    default=True,
    flag_value=False,
    help=(
        "Launch without arming a timer. The run is then recorded as "
        "unsupervised until something arms one."
    ),
)
@click.option(
    "--no-env-check",
    "env_check",
    is_flag=True,
    default=True,
    flag_value=False,
    help="Arm even though a scheduled tend would not inherit this shell's credentials.",
)
@click.option(
    "--log-root",
    type=Setting("log_root"),
    default=None,
    metavar="PATH",
    help=(
        "Root this machine keeps eval logs under. Used only where the "
        "definition names no log_dir, in which case this run writes to "
        f"<root>/<workspace name>. {overrides('log_root')}"
    ),
)
@click.option(
    "--no-log-root",
    is_flag=True,
    default=False,
    help="Keep this run's logs in the workspace, whatever root the machine configured.",
)
@click.option(
    "--log-store",
    type=Setting("log_store"),
    default=None,
    metavar="PATH|auto",
    help=(
        "Where to look for logs this run does not have to produce. Recorded "
        f"now; read when signoff can publish to it. {overrides('log_store')}"
    ),
)
@click.option(
    "--no-log-store",
    is_flag=True,
    default=False,
    help="Run against no log store, whatever this project or machine configured.",
)
@click.option(
    "--notification",
    type=Setting("notification"),
    default=None,
    metavar="URL|PATH",
    help=(
        "Where to post what this run cannot decide — an Apprise URL, several "
        "separated by commas, or an Apprise config file. Reaches every worker "
        f"too. {overrides('notification')}"
    ),
)
@click.option(
    "--no-notification",
    is_flag=True,
    default=False,
    help=(
        "Post nothing about this run. Silences Steward only — a worker waiting "
        "on a person still asks."
    ),
)
@shape_options
@tend_interval_option
@sync_options
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
    help="Output the delta and the first turn as JSON.",
)
@passthrough_options
def launch_command(
    definition: Path | None,
    definition_args: tuple[str, ...],
    no_args: bool,
    no_overrides: bool,
    definition_type: DefinitionType | None,
    accept_archive: bool,
    timer: bool,
    env_check: bool,
    log_root: str | bool | None,
    no_log_root: bool,
    log_store: str | bool | None,
    no_log_store: bool,
    notification: str | bool | None,
    no_notification: bool,
    max_workers: int | None,
    stall_after: int | None,
    samples_ramp: tuple[int, int] | bool | None,
    stuck_after: int | None,
    preauthorized: dict[str, str] | bool | None,
    tend_interval: int | None,
    sync: str | bool | None,
    no_sync: bool,
    no_break_claim: bool,
    output_json: bool,
    **passthrough: Any,
) -> None:
    """Start a run: capture the definition, commit it, arm a timer, tend once.

    DEFINITION is a Python file culminating in an eval_set() call, an Inspect Flow spec (Python or YAML), or a Hawk eval set config (YAML). Omitted, this workspace's own definition is used.

    Safe to run again. A second launch is the amend path — it re-captures, reports what changed, and refuses to commit anything that would move results out of logs/ unless you pass --accept-archive.
    """
    if no_args and definition_args:
        raise click.UsageError(
            "--no-args asks to capture with no arguments, and -A supplies one. "
            "Pass whichever you meant."
        )
    given_overrides = collect_overrides(passthrough)
    if no_overrides and given_overrides:
        raise click.UsageError(
            "--no-overrides asks to capture at the definition's own shape, and "
            f"--{sorted(given_overrides)[0].replace('_', '-')} changes it. "
            "Pass whichever you meant."
        )
    if no_sync and sync is not None:
        raise click.UsageError(
            "--no-sync asks to propagate nowhere, and --sync names a "
            "destination. Pass whichever you meant."
        )
    if no_log_root and log_root is not None:
        raise click.UsageError(
            "--no-log-root asks to keep the logs here, and --log-root names a "
            "root. Pass whichever you meant."
        )
    if no_log_store and log_store is not None:
        raise click.UsageError(
            "--no-log-store asks for no store, and --log-store names one. "
            "Pass whichever you meant."
        )
    if no_notification and notification is not None:
        raise click.UsageError(
            "--no-notification asks to post nowhere, and --notification names "
            "a channel. Pass whichever you meant."
        )

    workspace = find_workspace()
    resolved = definition or _own_definition(workspace)
    # **before the launch, because the launch settles the channel into this
    # process's own environment.** Asking afterwards would find whatever
    # `--notification` exported and report a durable channel where there is one
    # good for a single turn
    durable = _durable_channel(workspace)

    try:
        result = launch(
            workspace,
            resolved,
            # `{}` and `None` are different instructions here: an empty
            # mapping says *no arguments*, and `None` says *whatever the
            # committed manifest used*
            args={} if no_args else parse_args(definition_args),
            type=definition_type,
            accept_archive=accept_archive,
            timer=timer,
            env_check=env_check,
            log_root=False if no_log_root else log_root,
            log_store=False if no_log_store else log_store,
            notification=False if no_notification else notification,
            # `{}` and `None` differ here exactly as they do for `args`
            overrides={} if no_overrides else given_overrides,
            max_workers=max_workers,
            stall_after=stall_after,
            samples_ramp=samples_ramp,
            stuck_after=stuck_after,
            preauthorized=preauthorized,
            tend_interval=tend_interval,
            sync=False if no_sync else sync,
            break_stale=not no_break_claim,
        )
    except (LaunchError, *TURN_ERRORS) as ex:
        raise click.ClickException(str(ex)) from ex

    if isinstance(result, Held):
        _echo_held(result)

    if output_json:
        click.echo(_launch_json(result))
    else:
        _echo_launch(result, workspace.root)
        if result.committed and not no_notification:
            if durable is None:
                _echo_no_channel(one_launch=notification is not None)
            elif isinstance(durable, str) and not usable_channel(workspace, durable):
                _echo_unusable_channel()

    if not result.committed:
        # after the delta rather than instead of it, and an error rather than a
        # note: a refusal that exits zero is a refusal a script does not notice
        raise click.ClickException(_refusal(result.delta))


def _durable_channel(workspace: Workspace) -> str | bool | None:
    """What a scheduled tend will find: a channel, `False` for declined, or `None`.

    **The question is about a *scheduled* tend, which is why the flag does not count.** `--notification` shapes this launch's own turn and nothing after it: a timer inherits no environment, so the value is gone by the next fire. Asking `establish_channel` what is set right now would answer for this process, and after the launch has run that includes what the flag exported — so this is called before the launch and reads only the two spellings that survive it.

    **The target rather than a yes**, because a declaration is not a channel: a mistyped URL and a config file that has moved are both non-empty strings that reach nobody, and the caller checks (`usable_channel`). A declined workspace answers `False` and is asked nothing further — somebody who wrote `notification: false` has already said what they want.
    """
    try:
        directives = read_directives(workspace.directives)
    except (DirectivesError, OSError):
        # unreachable in practice, since `launch` raises on an unreadable file
        # before it commits. Treated as answered rather than guessed at: the
        # file is where the answer would have been, and this line's whole
        # content is a claim about it
        return False
    if directives.notification is not None:
        return directives.notification
    return os.environ.get(INSPECT_NOTIFICATION, "").strip() or None


def _echo_no_channel(*, one_launch: bool) -> None:
    """One line, where a run has just been launched and nothing will reach a person.

    **The one feature whose absence is silent by construction**, which is what earns it a line nothing else here gets: a run with no channel behaves exactly like a run with one until the night it needs somebody, and then it behaves exactly like a run that is going fine. Everything else worth saying at launch is already visible in what the launch printed.

    Launch only, and never an item. A note repeated every ten minutes is the nagging that trains a reader to ignore the channel this is advertising — and an operator who wrote `notification: false` has answered the question, so they do not hear it at all.

    Args:
        one_launch: Whether `--notification` was given. It is not silence, so the line says what is actually wrong: the channel lapses with the invocation, and the scheduled turns are the ones nobody is watching.
    """
    if one_launch:
        click.echo(
            "\n--notification applies to this launch only — a scheduled tend "
            "inherits no environment, so later turns will reach nobody. Put it "
            "in _steward.yaml or .env to make it stick."
        )
        return
    click.echo(
        "\nnothing will reach you if this run needs a person — set "
        "notification in _steward.yaml, or STEWARD_NOTIFICATION in .env, "
        "to an Apprise URL (slack://…, mailto://…)"
    )


def _echo_unusable_channel() -> None:
    """One line, where a channel is configured and resolves to nothing.

    **The same failure as no channel at all, arriving with a setting that says otherwise** — which makes it the worse of the two, because the operator has already done the thing they would be told to do. A mistyped scheme, a config file that has been renamed, a YAML file with nothing left in it after a bad edit: all three build an Apprise instance holding no targets, and nothing else says so until the night it matters.
    """
    click.echo(
        "\nthe notification setting resolves to no usable targets, so nothing "
        "will reach you if this run needs a person — check the URL, or the "
        "Apprise config file it names"
    )


def _own_definition(workspace: Workspace) -> Path:
    """This workspace's definition, found by name.

    Optional argument rather than required, because the ordinary case is a workspace with exactly one definition sitting in it and `steward launch` should be the whole command. An explicit path still wins, which is what a shared definition outside the workspace needs.
    """
    if (found := workspace.find_definition()) is None:
        raise click.ClickException(
            "this workspace has no definition — create one (evalset.py, "
            "flow.yaml, or hawk.yaml) or name one on the command line"
        )
    return found


def _echo_launch(result: Launch, root: Path) -> None:
    """Print a launch: what it would change, then what it did."""
    for line in delta_lines(result.delta, root=root):
        click.echo(line)

    if not result.committed:
        return

    click.echo("")
    if result.armed is not None:
        click.echo(
            f"armed {result.armed.scheduler} — tending every "
            f"{format_duration(result.armed.interval)}"
        )
    elif result.disarmed is not None:
        click.echo(
            f"disarmed {result.disarmed} — nothing will tend this run until "
            f"somebody does"
        )
    else:
        click.echo("no timer armed — nothing will tend this run until somebody does")

    if result.restored:
        click.echo(
            f"restored {_plural(len(result.restored), 'log')} from the archive "
            f"— that work does not run again"
        )

    # the startup-memory bound is not printed here: `echo_turn` below prints it
    # for every verb, and a launch always has a turn to echo, so a line of its
    # own only ever duplicated that one (found by running the M2 gate)
    for stop in result.stopped:
        click.echo(f"stopped worker {stop.worker} ({stop.outcome.value})")
    for failure in result.failures:
        click.echo(f"! {failure}")

    if result.turn is not None:
        click.echo("")
        echo_turn(result.turn)


def delta_lines(delta: Delta, *, root: Path | None = None) -> list[str]:
    """The delta as a person reads it.

    One line per kind rather than per task, because the decision the reader is making is about *kinds*: forty extended tasks is one fact, and forty lines of it buries the eight being archived underneath. The tasks themselves are in the manifest and in the turn that follows.

    Args:
        delta: What launching would change.
        root: The workspace, for shortening the directories a relocation names. Optional so that a caller with only a delta can still render one.

    Returns:
        The lines, in reading order.
    """
    if delta.empty:
        return ["nothing to change — this definition is already committed as it stands"]

    lines = ["launching this eval set:" if delta.first else "launching would:"]
    if (moved := delta.relocated) is not None:
        # first, and in sentences rather than in the table, because it is not a
        # fact about tasks: every row below can be empty while this one costs
        # the whole run
        lines.append(
            f"  the log directory moves: {_short(moved.old, root)} → "
            f"{_short(moved.new, root)}"
        )
        if moved.stranded:
            lines.append(
                f"    {_plural(moved.stranded, 'task')} with results in the old "
                f"one would run again in the new one, and those results would "
                f"be left where they are"
            )
        if moved.workers:
            lines.append(
                f"    {_plural(len(moved.workers), 'worker')} still writing to "
                f"the old one would be stopped"
            )
    if (slice := delta.reshaped) is not None:
        # also above the table, and for the same reason: every row can be empty
        # while this one re-runs the whole set. Without it a launch that changed
        # which samples run reports "nothing to change", because nothing the
        # rows are about did
        lines.append(f"  the samples change: {', '.join(slice.fields)}")
        if slice.affected:
            lines.append(
                f"    {_plural(slice.affected, 'task')} with results would run "
                f"again, and the results they have would be superseded"
            )
        if slice.workers:
            # the fleet costs the same here as it does under a relocation and
            # has to be said in the same place: this preview is what somebody
            # reads while deciding whether to accept an archiving change, and a
            # launch that also stops every worker should not disclose it
            # afterwards
            lines.append(
                f"    {_plural(len(slice.workers), 'worker')} running the old "
                f"one would be stopped"
            )
    for change, (verb, reason) in _LABELS.items():
        rows = delta.of(change)
        if not rows:
            continue
        samples = sum(row.samples for row in rows)
        detail = reason
        if change is Change.EXTEND:
            transitions = {row.epochs for row in rows if row.epochs is not None}
            detail = (
                f"epochs {min(before for before, _ in transitions)} → "
                f"{max(after for _, after in transitions)}"
                if transitions
                else "more samples than before"
            )
        counts = [_plural(samples, "sample")]
        if logs := sum(len(row.logs) for row in rows):
            counts.append(_plural(logs, "log"))
        if workers := sum(1 for row in rows if row.worker is not None):
            counts.append(f"{_plural(workers, 'worker')} stopped")
        tasks = _plural(len(rows), "task")
        lines.append(f"  {verb:<9} {tasks:>8}   {detail:<28} ({', '.join(counts)})")
    return lines


def _plural(count: int, noun: str) -> str:
    """A count and its noun, agreeing.

    One line of ceremony for a reason that is not cosmetic: this output is read once, by somebody deciding whether to pass `--accept-archive`, and *1 tasks would leave logs/* reads like a message from a tool that is not paying attention — at exactly the moment the reader is being asked to trust it.
    """
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _short(directory: str, root: Path | None) -> str:
    """A log directory as somebody reads it rather than as it resolves.

    A workspace's own `logs/` resolves to an absolute path two hundred characters long under a pytest temp root or a deep home directory, and the relocation message is precisely the one a person is reading while deciding whether to accept it — two of those side by side bury the sentence they are in. Anything outside the workspace, an S3 prefix included, is left exactly as it is: it is not this workspace's to abbreviate.
    """
    if root is None:
        return directory
    try:
        return str(Path(directory).relative_to(root))
    except ValueError:
        return directory


def _refusal(delta: Delta) -> str:
    """Why the gate said no, naming every reason rather than the first.

    Two conditions fail the same predicate and a launch can hit both at once — an edit that removes a task *and* moves the log directory. Reporting one would send somebody to fix it and be refused again for the other.
    """
    reasons: list[str] = []
    if delta.archiving:
        reasons.append(f"{_plural(len(delta.archiving), 'task')} would leave logs/")
    if (moved := delta.relocated) is not None:
        reasons.append(
            f"{_plural(moved.stranded, 'task')} would be left behind by the "
            f"log directory moving"
            if moved.stranded
            else "the log directory would move"
        )
    return (
        f"{' and '.join(reasons)}. Nothing has been committed — pass "
        f"--accept-archive to proceed."
    )


def _echo_held(held: Held) -> NoReturn:
    """Report a claim somebody else holds, and stop.

    Unlike a refused tend, this one *is* a problem — a tend that cannot run is a turn skipped and the next one covers it, where a launch that cannot run means the instruction somebody just typed did not happen — so it exits non-zero rather than reporting an outcome. `NoReturn` is what says so at the call site, where a `return` after this would otherwise read as a reachable path.
    """
    since = f" since {held.since}" if held.since else ""
    who = f"pid {held.pid}" if held.pid else "another process"
    click.echo(f"a {held.command or 'command'} holds the claim{since} ({who}).")
    if held.unbroken:
        click.echo(f"it looks wedged, and could not be cleared: {held.unbroken}")
    raise click.ClickException("nothing was launched — try again when it is done.")


def _launch_json(result: Launch) -> str:
    """A launch as JSON, for an agent rather than a person.

    The manifest is dumped by pydantic rather than by `dataclasses.asdict`, which does not recurse into a model and would render the whole eval set as one `str()` of a repr. Every other field is a dataclass and goes through the same path `turn_json` uses.
    """
    document = dataclasses.asdict(dataclasses.replace(result, manifest=result.manifest))
    document["manifest"] = result.manifest.model_dump(mode="json")
    return json.dumps(document, indent=2, default=str)
