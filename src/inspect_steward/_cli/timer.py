"""`steward timer` — arming the thing that makes the loop happen with nobody watching.

An agent is turn-based, so an agent-scheduled run is silent the moment no agent is in session — and silence is indistinguishable from a healthy run (agent.md §2). This is the floor under that: a system timer runs the mechanical half regardless.

**A verb group rather than a flag**, because three different callers need it at three different times. Step 16's `launch` arms as part of starting a run, step 26's signoff disarms as part of ending one, and an operator does either in the middle — and a `--timer` flag on `launch` would serve only the first of the three.

**Arming refuses when the timer would not inherit this shell's credentials.** A scheduled tend runs under a stripped environment, and the failure that produces is the worst one available: every ten minutes all night, a worker starts, authenticates against nothing, and writes a log that says so. The check is a diff rather than a requirement — see `_timer.env` — and `--no-env-check` is there for the case where running without them is the point.
"""

import json
import os

import click

from .._timer import (
    ORDER,
    TimerError,
    arm,
    disarm,
    explain_env,
    installed,
    unavailable_credentials,
)
from .._util.duration import DurationError, format_duration
from .._workspace import (
    Directives,
    DirectivesError,
    Workspace,
    ensure_gitignore,
    read_directives,
    resolve_interval,
)
from .turn import find_workspace


@click.group("timer")
def timer_command() -> None:
    """Arm, disarm, and inspect the timer that tends this run."""


@timer_command.command("arm")
@click.option(
    "--interval",
    default=None,
    help="How often to tend, e.g. `10m`. Overrides `_steward.yaml`.",
)
@click.option(
    "--scheduler",
    "name",
    type=click.Choice(list(ORDER)),
    default=None,
    help="Which scheduler to use. Detected when not given, preferring one that survives a reboot.",
)
@click.option(
    "--no-env-check",
    "env_check",
    is_flag=True,
    default=True,
    flag_value=False,
    help="Arm even though a scheduled tend would not inherit this shell's credentials.",
)
def arm_command(interval: str | None, name: str | None, env_check: bool) -> None:
    """Install a timer that tends this workspace on a schedule.

    Idempotent: an existing timer is removed first, so re-arming at a new interval or under a different scheduler leaves exactly one.
    """
    workspace = find_workspace()
    seconds = _interval(workspace, interval)

    # before anything below names `.env`, because a workspace created before that
    # entry existed does not have it and nothing re-runs `init` -- so telling
    # somebody to put their API keys in a path git would track is a hazard this
    # command introduced and has to close
    ignored = ensure_gitignore(workspace)

    if env_check:
        missing = unavailable_credentials(workspace.env, os.environ)
        if missing:
            raise click.ClickException(explain_env(missing, workspace.env))

    try:
        armament = arm(workspace, seconds, name=name)
    except TimerError as ex:
        raise click.ClickException(str(ex)) from ex

    click.echo(
        f"armed {armament.scheduler} — tending every "
        f"{format_duration(armament.interval)}"
    )
    click.echo(f"  {armament.description}")
    click.echo(f"  output goes to {workspace.timer_log}")
    if ignored:
        click.echo(f"  added to .gitignore: {', '.join(ignored)}")


@timer_command.command("disarm")
def disarm_command() -> None:
    """Remove this workspace's timer.

    Nothing else stops: workers in flight finish, and `steward tend` still works by hand. What ends is anything happening without somebody typing it, which every later `status` then reports.
    """
    workspace = find_workspace()
    try:
        removed = disarm(workspace)
    except TimerError as ex:
        raise click.ClickException(str(ex)) from ex

    if removed is None:
        click.echo("no timer was armed")
    else:
        click.echo(f"disarmed {removed} — nothing will tend this run automatically")


@timer_command.command("status")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the timer's state as JSON.",
)
def status_command(output_json: bool) -> None:
    """Say what is armed, and check that it is really there.

    The one command that asks the scheduler rather than the journal. Every other reader — a tend, a `status`, the item projection — goes on what arming recorded, because they run every few minutes and this costs a subprocess.
    """
    workspace = find_workspace()
    # the preference `_steward.yaml` expressed, not a resolved one: an operator
    # who armed a one-off `--interval 1m` against a file with no opinion has
    # not created a conflict for this to report
    wanted = _directives(workspace).tend_interval
    try:
        state = installed(workspace, wanted)
    except TimerError as ex:
        raise click.ClickException(str(ex)) from ex

    if output_json:
        click.echo(
            json.dumps(
                {
                    "scheduler": state.armed.scheduler if state.armed else None,
                    "interval": state.armed.interval if state.armed else None,
                    "armed_at": state.armed.ts if state.armed else None,
                    "present": state.present,
                    "wanted_interval": wanted,
                    "disagrees": state.disagrees,
                    "drifted": state.drifted,
                },
                indent=2,
            )
        )
        return

    if state.armed is None:
        click.echo("no timer is armed — `steward timer arm` installs one")
        return

    click.echo(
        f"{state.armed.scheduler}, tending every "
        f"{format_duration(state.armed.interval)} since {state.armed.ts}"
    )
    if state.disagrees:
        # the journal is what every turn trusts, so this is the run believing it
        # is supervised while it is not -- worth more than a line about a probe
        click.echo(
            f"  but {state.armed.scheduler} has no entry for it — something "
            f"removed the timer outside Steward. `steward timer arm` reinstalls it"
        )
    elif state.present is None:
        click.echo("  (could not be confirmed with the scheduler)")
    if state.drifted and wanted is not None:
        click.echo(
            f"  this workspace now asks for {format_duration(wanted)} — "
            f"`steward timer arm` applies it"
        )


def _directives(workspace: Workspace) -> Directives:
    try:
        return read_directives(workspace.directives)
    except DirectivesError as ex:
        raise click.ClickException(str(ex)) from ex


def _interval(workspace: Workspace, interval: str | None) -> int:
    """What this workspace should tend at: the flag, then `_steward.yaml`, then the default."""
    try:
        return resolve_interval(_directives(workspace), interval=interval)
    except DurationError as ex:
        raise click.ClickException(str(ex)) from ex
