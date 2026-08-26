"""launchd — the timer macOS actually has.

A user agent in `~/Library/LaunchAgents`, loaded into the GUI domain so it runs whenever the user is logged in. `StartInterval` takes seconds and needs no expression language, which makes this the one backend where the interval a person asked for is the interval installed.

**The plist is built by `plistlib` rather than by a template.** A workspace path can contain an ampersand, and a hand-written XML template would produce a file launchd rejects with a message about the wrong line of an encoded document.
"""

import os
import plistlib
import shutil
import sys
from pathlib import Path

from .entry import Runner, TimerEntry, TimerError, run_command

NAME = "launchd"

AGENTS = "Library/LaunchAgents"


def render_plist(entry: TimerEntry) -> bytes:
    """The launch agent, as a property list.

    `RunAtLoad` is false so that arming is not itself a tend: `launch` has just converged the run, and a second turn a millisecond later would find the claim held and refuse — a confusing first line in the timer log for no gain.

    Args:
        entry: What to schedule.

    Returns:
        The plist, encoded.
    """
    return plistlib.dumps(
        {
            "Label": entry.label,
            "ProgramArguments": entry.argv,
            "WorkingDirectory": str(entry.workspace),
            "StartInterval": entry.interval,
            "StandardOutPath": str(entry.output),
            "StandardErrorPath": str(entry.output),
            "RunAtLoad": False,
            "ProcessType": "Background",
        }
    )


class Launchd:
    """The macOS user-agent backend."""

    name = NAME

    def __init__(self, runner: Runner = run_command) -> None:
        self.runner = runner

    def usable(self, entry: TimerEntry) -> bool:
        return sys.platform == "darwin" and shutil.which("launchctl") is not None

    def plist(self, entry: TimerEntry) -> Path:
        return Path.home() / AGENTS / f"{entry.label}.plist"

    def describe(self, entry: TimerEntry) -> str:
        return f"a launchd user agent at {self.plist(entry)}"

    def arm(self, entry: TimerEntry) -> None:
        plist = self.plist(entry)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(render_plist(entry))

        # unconditionally, because bootstrapping over a loaded label fails and
        # re-arming at a new interval is the ordinary reason to arm twice. Not
        # loaded is the expected outcome here, so its exit code is discarded
        self.runner(["launchctl", "bootout", self._target(entry)], None)

        result = self.runner(
            ["launchctl", "bootstrap", self._domain(), str(plist)], None
        )
        if not result.ok:
            plist.unlink(missing_ok=True)
            raise TimerError(
                f"launchctl would not load {plist.name}: "
                f"{result.output or f'exit {result.code}'}"
            )

    def disarm(self, entry: TimerEntry) -> None:
        result = self.runner(["launchctl", "bootout", self._target(entry)], None)
        self.plist(entry).unlink(missing_ok=True)
        # a label that was not loaded is the state disarming wanted, so the
        # question is not whether launchctl complained but whether the agent is
        # still there -- asked of launchctl, because the plist has just been
        # removed and an unloading failure is precisely the case where the file
        # being gone means nothing (`Systemd.disarm` asks the same way)
        if not result.ok and self.armed(entry):
            raise TimerError(
                f"launchctl would not unload {entry.label}, and it is still "
                f"loaded: {result.output or f'exit {result.code}'}"
            )

    def armed(self, entry: TimerEntry) -> bool:
        return self.runner(["launchctl", "print", self._target(entry)], None).ok

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _target(self, entry: TimerEntry) -> str:
        return f"{self._domain()}/{entry.label}"


__all__ = ["NAME", "Launchd", "render_plist"]
