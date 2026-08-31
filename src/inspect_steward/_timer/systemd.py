"""systemd — the timer a modern Linux has, in its user manager.

Two units: a `oneshot` service that runs the tend and a timer that starts it. `--user` throughout, because Steward supervises one person's runs on one machine and a system unit would need root to install and would run as the wrong user anyway.

**`OnUnitActiveSec` rather than `OnCalendar`**, so the interval is a duration in seconds and not a calendar expression that has to be reverse-engineered from one. `Persistent` is deliberately off: a laptop that slept through four intervals should tend once when it wakes, which is what the next interval does anyway, rather than firing a backlog at a fleet that has moved on.
"""

import shlex
import shutil
import sys
from pathlib import Path

from .entry import Runner, TimerEntry, TimerError, run_command, shell_command

NAME = "systemd"

UNITS = ".config/systemd/user"


def render_service(entry: TimerEntry) -> str:
    """The unit that runs one tend.

    **The redirect is in `ExecStart` rather than in `StandardOutput=append:`**, because systemd opens that path itself, will not create a missing parent, and fails the unit when it cannot — see `shell_command`. An `ExecStartPre` mkdir would not help: the output is opened for the whole unit, before the first `ExecStart*` line runs.
    """
    return "\n".join(
        [
            "[Unit]",
            f"Description=Steward tend for {entry.workspace}",
            "",
            "[Service]",
            "Type=oneshot",
            f"WorkingDirectory={entry.workspace}",
            f"ExecStart=/bin/sh -c {shlex.quote(shell_command(entry))}",
            "",
        ]
    )


def render_timer(entry: TimerEntry) -> str:
    """The unit that decides when.

    `OnBootSec` as well as `OnUnitActiveSec`, because without the first the timer never fires at all after a reboot until something starts the service once.
    """
    return "\n".join(
        [
            "[Unit]",
            f"Description=Steward tend timer for {entry.workspace}",
            "",
            "[Timer]",
            f"OnBootSec={entry.interval}s",
            f"OnUnitActiveSec={entry.interval}s",
            f"Unit={entry.label}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


class Systemd:
    """The systemd user-manager backend."""

    name = NAME

    def __init__(self, runner: Runner = run_command) -> None:
        self.runner = runner

    def usable(self, entry: TimerEntry) -> bool:
        # a live user manager, not merely an installed binary: systemd is
        # present inside containers and on WSL where `--user` has nothing to
        # talk to, and this is the cheapest question that distinguishes them
        if sys.platform != "linux" or shutil.which("systemctl") is None:
            return False
        return self.runner(["systemctl", "--user", "show-environment"], None).ok

    def units(self, entry: TimerEntry) -> tuple[Path, Path]:
        """The service and timer unit files, in that order."""
        directory = Path.home() / UNITS
        return (
            directory / f"{entry.label}.service",
            directory / f"{entry.label}.timer",
        )

    def describe(self, entry: TimerEntry) -> str:
        return f"a systemd user timer, {entry.label}.timer"

    def arm(self, entry: TimerEntry) -> None:
        service, timer = self.units(entry)
        service.parent.mkdir(parents=True, exist_ok=True)
        service.write_text(render_service(entry), encoding="utf-8")
        timer.write_text(render_timer(entry), encoding="utf-8")

        self._reload()
        result = self.runner(
            ["systemctl", "--user", "enable", "--now", timer.name], None
        )
        if not result.ok:
            service.unlink(missing_ok=True)
            timer.unlink(missing_ok=True)
            self._reload()
            raise TimerError(
                f"systemctl would not enable {timer.name}: "
                f"{result.output or f'exit {result.code}'}"
            )

    def disarm(self, entry: TimerEntry) -> None:
        service, timer = self.units(entry)
        result = self.runner(
            ["systemctl", "--user", "disable", "--now", timer.name], None
        )
        service.unlink(missing_ok=True)
        timer.unlink(missing_ok=True)
        self._reload()
        # a unit that was already gone is the state disarming wanted; the files
        # are removed either way, so only a systemctl that failed while the
        # timer is still active is a real refusal
        if not result.ok and self.armed(entry):
            raise TimerError(
                f"systemctl would not disable {timer.name}: "
                f"{result.output or f'exit {result.code}'}"
            )

    def armed(self, entry: TimerEntry) -> bool:
        return self.runner(
            ["systemctl", "--user", "is-active", f"{entry.label}.timer"], None
        ).ok

    def _reload(self) -> None:
        self.runner(["systemctl", "--user", "daemon-reload"], None)


__all__ = ["NAME", "Systemd", "render_service", "render_timer"]
