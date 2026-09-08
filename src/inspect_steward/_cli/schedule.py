"""`steward schedule` — the agent's own recurring return, for a harness that cannot schedule one.

The runbook tells the agent to point *"whatever your harness has for recurring work"* at `steward collect`. Some harnesses have exactly that in session (Claude Code's wakeup, a `/loop`); the Codex CLI does not. This is the substitute: an OS scheduler entry that runs `<agent> exec` against `steward collect` on the tend interval, so the agent is called back to act on what a tend surfaced even with no session open.

**Distinct from `steward timer`, deliberately.** The tend timer runs the *mechanical* half and its absence is what `unsupervised` watches; this runs the *judgement* half, which the agent arms for itself and which nothing has to guarantee. So it is a sibling group with its own scheduler entry (labelled apart so neither disarms the other) and its own journal events — not a flag on `timer` or `collect`.

Like the tend timer, the scheduled collect runs under a stripped environment that reads the workspace's `.env` from its working directory; unlike it, it also needs its harness's own credentials, which live wherever that harness keeps them (`codex login` writes `~/.codex`). Steward does not vet either — it cannot tell one harness's auth model from another's, and a false refusal is worse than a schedule the operator can check with `status`.

**What the scheduler fires is the `run` wrapper, not `<agent> exec` directly.** A scheduler is not a supervisor — cron in particular starts a fresh process every interval regardless of the last — so a collect that outruns its interval would have a second agent stacked on top of it. `arm` schedules `steward schedule run`, which takes a non-blocking lock and only then execs the agent, skipping the tick if a previous collect still holds it (`_timer.collect`). The agent argv is resolved at fire time; arm still resolves it once to fail fast on a missing harness.
"""

import json
import shutil
import sys
from datetime import datetime, timezone

import click

from .._timer import (
    ORDER,
    TimerError,
    arm_agent,
    disarm_agent,
    guarded_collect,
    installed_agent,
    run_agent,
)
from .._util.duration import format_duration
from .._workspace import (
    Directives,
    DirectivesError,
    Workspace,
    ensure_gitignore,
    read_directives,
    resolve_interval,
)
from .options import tend_interval_option
from .turn import find_workspace

AGENTS = ("codex",)
"""The harnesses `steward schedule` knows how to drive. One for now; the mechanism is any `<agent> exec` that runs a prompt non-interactively."""

COLLECT_PROMPT = (
    "You are tending an eval run through Steward, on a timer, with no human "
    "watching this session. Run `steward collect` in this directory and read "
    "it. Follow `steward runbook`: act on whatever needs a decision "
    "(investigate, propose, rule, notify), put at most one question to the "
    "operator, and record what you do through the `steward` verbs. If nothing "
    "in the collection is for you, do nothing and send no message."
)
"""What a scheduled `<agent> exec` is told to do. Shipped with the code, not the workspace, so it cannot drift from the CLI it drives."""


@click.group("schedule")
def schedule_command() -> None:
    """Arm, disarm, and inspect the agent's recurring collect."""


@schedule_command.command("arm")
@click.option(
    "--agent",
    type=click.Choice(list(AGENTS)),
    default="codex",
    help="Which harness runs the scheduled collect.",
)
@tend_interval_option
@click.option(
    "--scheduler",
    "name",
    type=click.Choice(list(ORDER)),
    default=None,
    help="Which scheduler to use. Detected when not given, preferring one that survives a reboot.",
)
def arm_command(agent: str, tend_interval: int | None, name: str | None) -> None:
    """Schedule `<agent> exec` to collect and act on this workspace on a schedule.

    Idempotent: an existing schedule is removed first, so re-arming at a new interval or under a different scheduler leaves exactly one. Independent of the tend timer — arming this arms neither, and `steward timer` is unaffected.
    """
    workspace = find_workspace()
    seconds = _interval(workspace, tend_interval)
    # resolve the agent now so a missing harness fails at arm time rather than
    # silently every interval, but schedule the Steward wrapper rather than the
    # agent directly -- the wrapper is what takes the overlap lock (`_run`)
    _command(agent)
    command = _scheduled_command(agent)

    # the scheduled collect writes to `.steward/collect.log`; a workspace made
    # before `.steward/` was ignored would otherwise track it
    ignored = ensure_gitignore(workspace)

    try:
        armament = arm_agent(workspace, seconds, command, name=name)
    except TimerError as ex:
        raise click.ClickException(str(ex)) from ex

    click.echo(
        f"armed {armament.scheduler} — {agent} collects every "
        f"{format_duration(armament.interval)}"
    )
    click.echo(f"  {armament.description}")
    click.echo(f"  output goes to {workspace.collect_log}")
    if ignored:
        click.echo(f"  added to .gitignore: {', '.join(ignored)}")


@schedule_command.command("disarm")
def disarm_command() -> None:
    """Remove this workspace's scheduled collect.

    Nothing else stops: the tend timer keeps tending, and `steward collect` still works by hand. What ends is the agent being called back automatically.
    """
    workspace = find_workspace()
    try:
        removed = disarm_agent(workspace)
    except TimerError as ex:
        raise click.ClickException(str(ex)) from ex

    if removed is None:
        click.echo("no collect was scheduled")
    else:
        click.echo(f"disarmed {removed} — nothing will collect this run automatically")


@schedule_command.command("status")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output the schedule's state as JSON.",
)
def status_command(output_json: bool) -> None:
    """Say what is scheduled, and check that it is really there.

    Asks the scheduler rather than the journal, the same way `steward timer status` does, and for the same reason.
    """
    workspace = find_workspace()
    wanted = _directives(workspace).tend_interval
    try:
        state = installed_agent(workspace, wanted)
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
        click.echo("no collect is scheduled — `steward schedule arm` installs one")
        return

    click.echo(
        f"{state.armed.scheduler}, collecting every "
        f"{format_duration(state.armed.interval)} since {state.armed.ts}"
    )
    if state.disagrees:
        click.echo(
            f"  but {state.armed.scheduler} has no entry for it — something "
            f"removed the schedule outside Steward. `steward schedule arm` reinstalls it"
        )
    elif state.present is None:
        click.echo("  (could not be confirmed with the scheduler)")
    if state.drifted and wanted is not None:
        click.echo(
            f"  this workspace now asks for {format_duration(wanted)} — "
            f"`steward schedule arm` applies it"
        )


@schedule_command.command("run", hidden=True)
@click.option(
    "--agent",
    type=click.Choice(list(AGENTS)),
    default="codex",
    help="Which harness to run under the overlap lock.",
)
def run_command(agent: str) -> None:
    """Run one scheduled collect under the overlap lock (what the scheduler fires).

    Not for operators: `arm` schedules this rather than the agent directly, so that a collect outrunning its interval is skipped rather than stacked on top of the one still running. Exits `0` on a skip, which is not a failure but the guard doing its job — so the scheduler does not treat an ordinary busy interval as a fault.
    """
    workspace = find_workspace()
    # the scheduler's `mkdir -p` already makes `.steward/`, but a hand-run of
    # this command need not have, and the lock's parent must exist to open it
    workspace.collect_lock.parent.mkdir(parents=True, exist_ok=True)
    argv = _command(agent)

    def on_skip() -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        click.echo(
            f"{stamp} collect skipped: a previous {agent} collect is still "
            f"running (it holds {workspace.collect_lock})"
        )

    raise SystemExit(
        guarded_collect(workspace.collect_lock, argv, runner=run_agent, on_skip=on_skip)
    )


def _scheduled_command(agent: str) -> list[str]:
    """The argv the scheduler actually runs: this Steward wrapper, not the agent.

    Running `schedule run` rather than `<agent> exec` directly is what puts the overlap lock (`_timer.collect.guarded_collect`) in front of every fire, uniformly across backends — cron, which starts a process every interval regardless, most of all. The interpreter is absolute for the same reason `_timer.entry` uses it: a scheduled command inherits almost no `PATH`.
    """
    return [
        sys.executable,
        "-m",
        "inspect_steward",
        "schedule",
        "run",
        "--agent",
        agent,
    ]


def _command(agent: str) -> list[str]:
    """The argv a scheduled collect runs, with the harness resolved to an absolute path.

    Absolute because a scheduled command inherits almost no `PATH` (`_timer.entry`), so a bare `codex` would not be found. Resolving it here also means a harness that is not installed is caught at arm time rather than failing silently every interval.
    """
    program = shutil.which(agent)
    if program is None:
        raise click.ClickException(
            f"`{agent}` is not on PATH here, so a scheduled collect could not "
            f"run it — install it, or drive collect from your own harness"
        )
    if agent == "codex":
        # **The sandbox and the approval gate both come off, deliberately.** A
        # scheduled collect runs with nobody in the session: it has to reach the
        # run's log store to observe it -- frequently S3, which the default
        # `workspace-write` sandbox blocks along with the rest of the network, so
        # a collect there hangs and times out rather than reading anything -- and
        # it has to carry out `steward` verbs without pausing for an approval no
        # one is there to give. Both are the operator's call, made by arming this
        # on the machine that will run it.
        return [
            program,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            COLLECT_PROMPT,
        ]
    # the Choice gates this; a new AGENTS entry without a branch is the bug it catches
    raise click.ClickException(f"no scheduled-collect command is defined for {agent}")


def _directives(workspace: Workspace) -> Directives:
    try:
        return read_directives(workspace.directives)
    except DirectivesError as ex:
        raise click.ClickException(str(ex)) from ex


def _interval(workspace: Workspace, tend_interval: int | None) -> int:
    """The collect interval: the flag, then `_steward.yaml` or its variable, then the default — the tend interval, since the agent collects on roughly the tend's cadence."""
    return resolve_interval(_directives(workspace), tend_interval=tend_interval)
