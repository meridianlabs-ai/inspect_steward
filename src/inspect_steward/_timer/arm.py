"""Arming and disarming, as a workspace experiences them.

The backends know how to install a timer; this is what makes doing so a fact about a *run* rather than about a machine. Three things happen here that no backend does:

**The journal records it.** A scheduler cannot report its own absence, and probing one costs a subprocess on every turn — so the fact that a timer was installed is written down once, and every later turn compares that record against how long it has actually been since a tend. That is the only way *supervision stopped* is ever noticed (`_tend.items`, `unsupervised`).

**Arming is idempotent, and disarms first.** Re-arming at a new interval is the ordinary second reason to arm, and every backend has a different way of failing when an entry already exists. Removing the recorded one first makes all three behave the same, and also makes *switch to a different backend* work — the record says which one to remove, so a run armed under cron and re-armed under launchd does not leave a crontab line firing.

**No claim is taken.** Arming touches the machine and appends one event; it changes nothing a tend is converging. Taking the claim would mean an operator could not arm a timer while the fleet is up, which is the moment they most often want to.
"""

from dataclasses import dataclass

from .._workspace import (
    ARMED,
    DISARMED,
    Armed,
    Workspace,
    append_event,
    read_armed,
    read_journal,
)
from .entry import Runner, TimerEntry, TimerError, run_command, timer_entry
from .scheduler import Scheduler, detect, scheduler


@dataclass(frozen=True)
class Armament:
    """What arming installed."""

    scheduler: str
    interval: int
    label: str
    description: str
    """What was installed, in words, for a person reading the command's output."""


def entry_for(workspace: Workspace, interval: int) -> TimerEntry:
    """This workspace's entry, as every backend needs it described."""
    return timer_entry(workspace.root, interval, output=workspace.timer_log)


def recorded(workspace: Workspace) -> Armed | None:
    """What the journal says is armed, without asking any scheduler.

    Args:
        workspace: The workspace.

    Returns:
        The timer in force, or `None` where none was armed.

    Raises:
        TimerError: The journal could not be read. **An unreadable journal is not *nothing is armed*, and the difference is the whole reason this raises.** This fold is the only record of what was installed, so a `disarm` that read the error as an empty history would print *no timer was armed* while the timer it could not see goes on tending every interval — and would leave nothing able to find it again. A missing journal is still an empty history; an unreadable one is an unanswered question.
    """
    try:
        return read_armed(read_journal(workspace.journal).events)
    except OSError as ex:
        raise TimerError(
            f"this workspace's journal could not be read, so what timer is "
            f"armed here is unknown: {ex}"
        ) from ex


def arm(
    workspace: Workspace,
    interval: int,
    *,
    name: str | None = None,
    runner: Runner | None = None,
) -> Armament:
    """Install a timer for this workspace and record that it exists.

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
    entry = entry_for(workspace, interval)

    runner = runner or run_command
    chosen = _choose(entry, name, runner)
    disarm(workspace, runner=runner)
    chosen.arm(entry)

    try:
        append_event(
            workspace.journal,
            ARMED,
            scheduler=chosen.name,
            interval=interval,
            label=entry.label,
        )
    except OSError as ex:
        # **An installed timer that nothing recorded is one nothing can remove.**
        # This event is the only record of which backend holds the entry, so
        # without it `disarm` has nowhere to look and the machine goes on tending
        # every interval until somebody edits a crontab by hand. Undoing the
        # arming is the only exit that leaves the two halves agreeing -- and if
        # that fails too, its error is the one worth surfacing, since it is the
        # one that names something still installed
        chosen.disarm(entry)
        raise TimerError(
            f"the timer was installed and then removed again, because the "
            f"journal that has to record it could not be written: {ex}"
        ) from ex

    return Armament(
        scheduler=chosen.name,
        interval=interval,
        label=entry.label,
        description=chosen.describe(entry),
    )


def disarm(workspace: Workspace, *, runner: Runner | None = None) -> str | None:
    """Remove the timer this workspace recorded, if it recorded one.

    Silent when nothing is armed, because that is also what `arm` calls to make itself idempotent and because *there was no timer* is the state a disarm wanted either way.

    Args:
        workspace: The workspace.
        runner: How backends reach the system.

    Returns:
        The backend that was removed, or `None` where nothing was armed.

    Raises:
        TimerError: The scheduler would not remove it.
    """
    current = recorded(workspace)
    if current is None:
        return None

    entry = entry_for(workspace, current.interval)
    scheduler(current.scheduler, runner=runner or run_command).disarm(entry)
    append_event(workspace.journal, DISARMED, scheduler=current.scheduler)
    return current.scheduler


@dataclass(frozen=True)
class Installed:
    """What `steward timer status` found, from both directions."""

    armed: Armed | None
    """What the journal recorded, which is what every turn believes."""

    present: bool | None
    """Whether that backend actually still holds the entry, or `None` where nothing was recorded to probe for."""

    interval: int | None
    """What `_steward.md` asks for now, or `None` where it asks for nothing.

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


def installed(
    workspace: Workspace, interval: int | None, *, runner: Runner | None = None
) -> Installed:
    """Read the record and then ask the scheduler whether it is true.

    The one place that pays for a probe. Every other reader — a tend, a `status`, the item projection — goes on the journal alone, because this costs a subprocess and they run every ten minutes.

    Args:
        workspace: The workspace.
        interval: What the workspace currently asks for, for the drift comparison.
        runner: How backends reach the system.

    Returns:
        Both answers, and whether they agree.
    """
    current = recorded(workspace)
    present: bool | None = None
    if current is not None:
        try:
            backend = scheduler(current.scheduler, runner=runner or run_command)
            present = backend.armed(entry_for(workspace, current.interval))
        except TimerError:
            # a backend this version does not know, or one whose command is
            # gone. Unknown rather than absent: claiming the timer is missing
            # would be a stronger statement than anything was learned here
            present = None
    return Installed(armed=current, present=present, interval=interval)


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
    "arm",
    "disarm",
    "entry_for",
    "installed",
    "recorded",
]
