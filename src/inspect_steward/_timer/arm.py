"""Arming and disarming, as a workspace experiences them.

The backends know how to install a timer; this is what makes doing so a fact about a *run* rather than about a machine. Three things happen here that no backend does:

**The journal records it.** A scheduler cannot report its own absence, and probing one costs a subprocess on every turn — so the fact that a timer was installed is written down once, and every later turn compares that record against how long it has actually been since a tend. That is the only way *supervision stopped* is ever noticed (`_tend.items`, `unsupervised`).

**Arming is idempotent, and disarms first.** Re-arming at a new interval is the ordinary second reason to arm, and every backend has a different way of failing when an entry already exists. Removing the recorded one first makes all three behave the same, and also makes *switch to a different backend* work — the record says which one to remove, so a run armed under cron and re-armed under launchd does not leave a crontab line firing.

**No claim is taken.** Arming touches the machine and appends one event; it changes nothing a tend is converging. Taking the claim would mean an operator could not arm a timer while the fleet is up, which is the moment they most often want to.
"""

from dataclasses import dataclass
from typing import Callable

from .._workspace import (
    AGENT_ARMED,
    AGENT_DISARMED,
    ARMED,
    DISARMED,
    Armed,
    Workspace,
    append_event,
    read_agent_armed,
    read_armed,
    read_journal,
)
from .entry import (
    Runner,
    TimerEntry,
    TimerError,
    agent_timer_entry,
    run_command,
    timer_entry,
)
from .scheduler import Scheduler, detect, scheduler


@dataclass(frozen=True)
class Armament:
    """What arming installed."""

    scheduler: str
    interval: int
    label: str
    description: str
    """What was installed, in words, for an operator reading the command's output."""


def entry_for(workspace: Workspace, interval: int) -> TimerEntry:
    """This workspace's tend-timer entry, as every backend needs it described."""
    return timer_entry(workspace.root, interval, output=workspace.timer_log)


def agent_entry_for(
    workspace: Workspace, interval: int, command: list[str] | None = None
) -> TimerEntry:
    """This workspace's agent-collect entry, as every backend needs it described.

    `command` is what `arm_agent` schedules; a disarm or a status probe needs only the entry's label, so it passes nothing and the argv is left empty.
    """
    return agent_timer_entry(
        workspace.root, interval, output=workspace.collect_log, command=command or []
    )


def _recorded(workspace: Workspace, fold: Callable[..., Armed | None]) -> Armed | None:
    """What the journal's `fold` says is armed, without asking any scheduler.

    Raises:
        TimerError: The journal could not be read. **An unreadable journal is not *nothing is armed*, and the difference is the whole reason this raises.** This fold is the only record of what was installed, so a `disarm` that read the error as an empty history would print *no timer was armed* while the entry it could not see goes on firing every interval — and would leave nothing able to find it again. A missing journal is still an empty history; an unreadable one is an unanswered question.
    """
    try:
        return fold(read_journal(workspace.journal).events)
    except OSError as ex:
        raise TimerError(
            f"this workspace's journal could not be read, so what is "
            f"armed here is unknown: {ex}"
        ) from ex


def recorded(workspace: Workspace) -> Armed | None:
    """What the journal says is armed as the tend timer. See `_recorded`."""
    return _recorded(workspace, read_armed)


def recorded_agent(workspace: Workspace) -> Armed | None:
    """What the journal says is armed as the agent collect. See `_recorded`."""
    return _recorded(workspace, read_agent_armed)


def _arm(
    workspace: Workspace,
    entry: TimerEntry,
    name: str | None,
    runner: Runner,
    *,
    armed_event: str,
    noun: str,
    predisarm: Callable[[], str | None],
) -> Armament:
    """Install `entry` under a chosen backend and record it under `armed_event`.

    Shared by `arm` (the tend timer) and `arm_agent` (the agent collect). `predisarm` is the matching disarm, called first so each kind's arming stays idempotent without either touching the other's entry.
    """
    chosen = _choose(entry, name, runner)
    predisarm()
    chosen.arm(entry)

    try:
        append_event(
            workspace.journal,
            armed_event,
            scheduler=chosen.name,
            interval=entry.interval,
            label=entry.label,
        )
    except OSError as ex:
        # **An installed entry that nothing recorded is one nothing can remove.**
        # This event is the only record of which backend holds the entry, so
        # without it a disarm has nowhere to look and the machine goes on firing
        # every interval until somebody edits a crontab by hand. Undoing the
        # arming is the only exit that leaves the two halves agreeing -- and if
        # that fails too, its error is the one worth surfacing, since it is the
        # one that names something still installed
        chosen.disarm(entry)
        raise TimerError(
            f"the {noun} was installed and then removed again, because the "
            f"journal that has to record it could not be written: {ex}"
        ) from ex

    return Armament(
        scheduler=chosen.name,
        interval=entry.interval,
        label=entry.label,
        description=chosen.describe(entry),
    )


def _disarm(
    workspace: Workspace,
    runner: Runner,
    *,
    current: Armed | None,
    entry: TimerEntry | None,
    disarmed_event: str,
) -> str | None:
    """Remove `entry` from the backend `current` names, and record the disarm.

    Shared by `disarm` and `disarm_agent`. Silent when `current` is `None` — which is also what an arming calls to stay idempotent, and *there was nothing armed* is the state a disarm wanted either way. `entry` is `None` exactly when `current` is.
    """
    if current is None or entry is None:
        return None
    scheduler(current.scheduler, runner=runner).disarm(entry)
    append_event(workspace.journal, disarmed_event, scheduler=current.scheduler)
    return current.scheduler


def arm(
    workspace: Workspace,
    interval: int,
    *,
    name: str | None = None,
    runner: Runner | None = None,
) -> Armament:
    """Install a tend timer for this workspace and record that it exists.

    Args:
        workspace: The workspace to supervise.
        interval: Seconds between tends.
        name: A specific backend, or `None` to detect one. A named backend that cannot be used here is refused rather than substituted — somebody who asked for systemd wants to know it is absent, not to find out three days later that something else was installed.
        runner: How backends reach the system.

    Returns:
        What was installed.

    Raises:
        TimerError: The named backend cannot be used here, the scheduler refused, or the journal that has to record the arming could not be written.
    """
    runner = runner or run_command
    return _arm(
        workspace,
        entry_for(workspace, interval),
        name,
        runner,
        armed_event=ARMED,
        noun="timer",
        predisarm=lambda: disarm(workspace, runner=runner),
    )


def arm_agent(
    workspace: Workspace,
    interval: int,
    command: list[str],
    *,
    name: str | None = None,
    runner: Runner | None = None,
) -> Armament:
    """Install a recurring agent collect for this workspace and record that it exists.

    The `arm` counterpart for the agent half of the loop: an independent scheduler entry that runs `command` (an `<agent> exec` invocation) on the same interval, taken down separately and never seen by the tend timer's supervision.

    Args:
        workspace: The workspace.
        interval: Seconds between collects.
        command: The argv to schedule, with an absolute program path.
        name: A specific backend, or `None` to detect one.
        runner: How backends reach the system.

    Returns:
        What was installed.

    Raises:
        TimerError: The named backend cannot be used here, the scheduler refused, or the journal that has to record the arming could not be written.
    """
    runner = runner or run_command
    return _arm(
        workspace,
        agent_entry_for(workspace, interval, command),
        name,
        runner,
        armed_event=AGENT_ARMED,
        noun="schedule",
        predisarm=lambda: disarm_agent(workspace, runner=runner),
    )


def disarm(workspace: Workspace, *, runner: Runner | None = None) -> str | None:
    """Remove the tend timer this workspace recorded, if it recorded one.

    Args:
        workspace: The workspace.
        runner: How backends reach the system.

    Returns:
        The backend that was removed, or `None` where nothing was armed.

    Raises:
        TimerError: The scheduler would not remove it.
    """
    current = recorded(workspace)
    entry = entry_for(workspace, current.interval) if current else None
    return _disarm(
        workspace,
        runner or run_command,
        current=current,
        entry=entry,
        disarmed_event=DISARMED,
    )


def disarm_agent(workspace: Workspace, *, runner: Runner | None = None) -> str | None:
    """Remove the agent collect this workspace recorded, if it recorded one.

    Args:
        workspace: The workspace.
        runner: How backends reach the system.

    Returns:
        The backend that was removed, or `None` where nothing was armed.

    Raises:
        TimerError: The scheduler would not remove it.
    """
    current = recorded_agent(workspace)
    entry = agent_entry_for(workspace, current.interval) if current else None
    return _disarm(
        workspace,
        runner or run_command,
        current=current,
        entry=entry,
        disarmed_event=AGENT_DISARMED,
    )


@dataclass(frozen=True)
class Installed:
    """What `steward timer status` found, from both directions."""

    armed: Armed | None
    """What the journal recorded, which is what every turn believes."""

    present: bool | None
    """Whether that backend actually still holds the entry, or `None` where nothing was recorded to probe for."""

    interval: int | None
    """What `_steward.yaml` asks for now, or `None` where it asks for nothing.

    The *expressed* preference rather than a resolved one, for the same reason `items.Supervision` carries it that way: an operator who armed a one-off `--interval 1m` against a file with no opinion has not created a conflict, and comparing against Steward's default would invent one.
    """

    @property
    def disagrees(self) -> bool:
        """The record says a timer is installed and the scheduler says otherwise.

        Somebody edited a crontab by hand, or removed a launch agent, or a `systemctl --user` session went away. Worth reporting distinctly: the journal is what every turn trusts, so a disagreement means the run believes it is supervised and is not.
        """
        return self.armed is not None and self.present is False

    @property
    def drifted(self) -> bool:
        """The installed interval is not the one the workspace asks for."""
        return (
            self.armed is not None
            and self.interval is not None
            and self.armed.interval != self.interval
        )


def _installed(
    workspace: Workspace,
    interval: int | None,
    current: Armed | None,
    entry: TimerEntry | None,
    runner: Runner,
) -> Installed:
    """Pair the journal record with a scheduler probe. Shared by `installed`/`installed_agent`."""
    present: bool | None = None
    if current is not None and entry is not None:
        try:
            present = scheduler(current.scheduler, runner=runner).armed(entry)
        except TimerError:
            # a backend this version does not know, or one whose command is
            # gone. Unknown rather than absent: claiming the entry is missing
            # would be a stronger statement than anything was learned here
            present = None
    return Installed(armed=current, present=present, interval=interval)


def installed(
    workspace: Workspace, interval: int | None, *, runner: Runner | None = None
) -> Installed:
    """Read the tend-timer record and then ask the scheduler whether it is true.

    The one place that pays for a probe. Every other reader — a tend, a `status`, the item projection — goes on the journal alone, because this costs a subprocess and they run every ten minutes.

    Args:
        workspace: The workspace.
        interval: What the workspace currently asks for, for the drift comparison.
        runner: How backends reach the system.

    Returns:
        Both answers, and whether they agree.
    """
    current = recorded(workspace)
    entry = entry_for(workspace, current.interval) if current else None
    return _installed(workspace, interval, current, entry, runner or run_command)


def installed_agent(
    workspace: Workspace, interval: int | None, *, runner: Runner | None = None
) -> Installed:
    """Read the agent-collect record and then ask the scheduler whether it is true.

    The `installed` counterpart for `steward schedule status`.

    Args:
        workspace: The workspace.
        interval: What the workspace currently asks for, for the drift comparison.
        runner: How backends reach the system.

    Returns:
        Both answers, and whether they agree.
    """
    current = recorded_agent(workspace)
    entry = agent_entry_for(workspace, current.interval) if current else None
    return _installed(workspace, interval, current, entry, runner or run_command)


def _choose(entry: TimerEntry, name: str | None, runner: Runner) -> Scheduler:
    if name is None:
        return detect(entry, runner=runner)
    chosen = scheduler(name, runner=runner)
    if not chosen.usable(entry):
        raise TimerError(
            f"{name} cannot run a timer here — "
            f"`steward timer status` says which scheduler this machine has"
        )
    return chosen


__all__ = [
    "Armament",
    "Installed",
    "agent_entry_for",
    "arm",
    "arm_agent",
    "disarm",
    "disarm_agent",
    "entry_for",
    "installed",
    "installed_agent",
    "recorded",
    "recorded_agent",
]
