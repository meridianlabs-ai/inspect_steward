"""What every scheduler is given, and how every scheduler talks to the system.

A backend is two halves and only one of them can run in a test suite. **Rendering** a plist, a systemd unit, or a crontab line is a pure function of a `TimerEntry`, so it is tested exhaustively. **Activating** it runs `launchctl`, `systemctl`, or `crontab` against the machine the tests are running on, so it is never executed in one — every backend takes a `Runner`, and a test asserts the argv it built rather than what the argv did.

That seam is the whole design of this package. It is also why `TimerEntry` exists at all: three backends that each computed their own label, command, and log path from a `Workspace` would drift, and the one place they must not drift is the label, which is what makes `disarm` remove this workspace's entry and not the neighbouring workspace's.
"""

import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable, Sequence

LABEL_PREFIX = "steward"
"""Opens every label, so a person reading `launchctl list` or their own crontab can see what put it there."""

MODULE = "inspect_steward"
"""Run as `python -m inspect_steward tend`. The console script would be shorter, but it lives in a venv's `bin/` that a stripped cron environment has no reason to have on `PATH`, and an absolute interpreter plus a module name needs no `PATH` at all."""


class TimerError(Exception):
    """A timer could not be armed, disarmed, or inspected.

    The scheduler refused, or is not the one this machine has. Raised rather than reported because there is no degraded form of *the run is supervised* — a half-armed timer that nobody is told about is the exact failure this step exists to prevent.
    """


@dataclass(frozen=True)
class Completed:
    """What a command did. Enough to decide, and not a `CompletedProcess`, so a test's fake runner is three lines."""

    code: int
    output: str
    """stdout and stderr together. Schedulers say useful things on both and a reader wants whichever one it was."""

    @property
    def ok(self) -> bool:
        return self.code == 0


Runner = Callable[[Sequence[str], str | None], Completed]
"""How a backend reaches the system: argv, optional stdin, and what came back."""


def run_command(argv: Sequence[str], stdin: str | None = None) -> Completed:
    """Run a command, capturing everything it said.

    The default `Runner`. Never raises on a nonzero exit — `launchctl bootout` on an entry that is not loaded is a routine part of arming idempotently, and a backend decides which codes matter.

    Args:
        argv: The command.
        stdin: Text to write to its input, where the command reads one (`crontab -`).

    Returns:
        Exit code and combined output.

    Raises:
        TimerError: The command does not exist. That is not a failed run — it means the scheduler this backend speaks to is not installed, which the caller must not mistake for a refusal.
    """
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as ex:
        raise TimerError(f"could not run {argv[0]}: {ex}") from ex
    return Completed(
        code=completed.returncode,
        output=(completed.stdout + completed.stderr).strip(),
    )


@dataclass(frozen=True)
class TimerEntry:
    """One workspace's scheduled tend, as any backend needs to describe it."""

    workspace: Path
    """Workspace root, resolved. Also the working directory the tend runs in, which is what lets `.env` and `Workspace.find` both work under a scheduler that starts in `/`."""

    interval: int
    """Seconds between tends."""

    label: str
    """This entry's name, unique per workspace. See `entry_label`."""

    argv: list[str]
    """The command a scheduler runs."""

    output: Path
    """Where the command's stdout and stderr go."""


def entry_label(workspace: Path) -> str:
    """This workspace's name in a scheduler's namespace.

    Derived from the path rather than from the directory's name, because three workspaces called `sweep` on one machine are the ordinary case and a label collision means arming the second one silently replaces the first. Hashed rather than escaped: a scheduler label has to survive being a filename, a systemd unit name, and a launchd domain target, and the intersection of what those three accept is narrow enough that sanitising a real path is guesswork.

    Args:
        workspace: Workspace root.

    Returns:
        A label like `steward-3f9a1c22b8d0`.
    """
    digest = sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()
    return f"{LABEL_PREFIX}-{digest[:12]}"


def timer_entry(workspace: Path, interval: int, *, output: Path) -> TimerEntry:
    """Describe a workspace's scheduled tend.

    Args:
        workspace: Workspace root.
        interval: Seconds between tends.
        output: Where the tend's output goes (`Workspace.timer_log`).

    Returns:
        The entry every backend renders from.
    """
    root = Path(workspace).resolve()
    return TimerEntry(
        workspace=root,
        interval=interval,
        label=entry_label(root),
        # the running interpreter, absolutely: a scheduled command inherits
        # almost no PATH, and `python` there is either absent or the wrong one
        argv=[sys.executable, "-m", MODULE, "tend"],
        output=output,
    )
