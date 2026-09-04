"""Which timer this machine gets, and the shape all three of them share.

**The order is by what each one survives.** launchd and systemd survive a reboot; cron survives a logout but not a reboot's worth of nothing being there to re-arm it. All three survive the terminal that armed them, which is the property that matters and the reason none of them is a process Steward owns.

**Detection can fail, and that is deliberate.** An earlier version had a fourth backend — a detached process of Steward's own, sleeping and running `tend` — which made `detect` unable to fail and so made [execution.md](execution.md)'s *"if neither can be armed, the launch fails"* unreachable. It was removed: it duplicated cron less well, it did not survive a reboot, and it was the one backend where a snapshot of the arming environment shadowed `.env` — a second credential model that every later step touching credentials would have had to reason about. A machine with no scheduler is now told so, and hand-driving stays available through `--no-timer`, which has the merit of making an unsupervised run *look* unsupervised.

Detection is per-entry rather than per-machine, because cron's usability depends on the interval: a seven-minute interval is not expressible in a crontab, so a host where cron is the only scheduler cannot take one.
"""

from typing import Protocol

from .cron import Cron
from .entry import Runner, TimerEntry, TimerError, run_command
from .launchd import Launchd
from .systemd import Systemd


class Scheduler(Protocol):
    """What every backend does.

    Four methods, and the split between them is what makes the package testable: `arm` and `disarm` reach the machine, while what they *install* is rendered by a pure function each backend exposes separately.
    """

    name: str

    def usable(self, entry: TimerEntry) -> bool:
        """Whether this backend can schedule this entry on this machine."""
        ...

    def describe(self, entry: TimerEntry) -> str:
        """What was installed, in words, for an operator reading a command's output."""
        ...

    def arm(self, entry: TimerEntry) -> None:
        """Install the timer, replacing any this backend already holds for the entry."""
        ...

    def disarm(self, entry: TimerEntry) -> None:
        """Remove it. Removing one that is not there is not an error."""
        ...

    def armed(self, entry: TimerEntry) -> bool:
        """Whether the backend currently holds this entry. Costs a subprocess."""
        ...


ORDER = ("launchd", "systemd", "cron")
"""Preference order, by what each one survives. See the module docstring."""


def scheduler(name: str, *, runner: Runner = run_command) -> Scheduler:
    """One backend by name.

    Args:
        name: One of `ORDER`.
        runner: How it reaches the system. Tests pass a fake.

    Returns:
        The backend, whether or not this machine can use it.

    Raises:
        TimerError: No such backend.
    """
    match name:
        case "launchd":
            return Launchd(runner)
        case "systemd":
            return Systemd(runner)
        case "cron":
            return Cron(runner)
        case _:
            raise TimerError(
                f"'{name}' is not a scheduler Steward knows — "
                f"choose one of {', '.join(ORDER)}"
            )


def schedulers(*, runner: Runner = run_command) -> list[Scheduler]:
    """Every backend, in preference order."""
    return [scheduler(name, runner=runner) for name in ORDER]


def detect(entry: TimerEntry, *, runner: Runner = run_command) -> Scheduler:
    """The best backend this machine can use for this entry.

    Args:
        entry: What is to be scheduled — the interval matters, since cron cannot express every one.
        runner: How backends reach the system.

    Returns:
        The first usable backend.

    Raises:
        TimerError: No scheduler here can run this entry, naming each one and why. That is a real outcome rather than a defensive branch — a container with no cron reaches it — and it is what makes an unsupervised run something somebody chose rather than something a machine decided quietly.
    """
    for candidate in schedulers(runner=runner):
        if candidate.usable(entry):
            return candidate
    raise TimerError(
        "no scheduler on this machine can run a timer "
        f"(tried {', '.join(ORDER)}) — install cron, or drive the run by hand "
        f"with `steward launch --no-timer` and accept that it is unsupervised"
    )


__all__ = ["ORDER", "Scheduler", "detect", "scheduler", "schedulers"]
