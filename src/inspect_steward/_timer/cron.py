"""cron — the fallback that exists everywhere and shares its file with everyone.

Two things about cron are different in kind from the other backends, and both shape this module.

**The crontab is one file per user, and `crontab -` replaces all of it.** There is no *add my line* primitive. So arming is read-modify-write over a block between two marker comments, and every line outside those markers is copied through byte for byte. That makes Steward safe against its own re-arming and against other tools' entries — but not against a concurrent `crontab -e` in another terminal, which will overwrite this or be overwritten by it. Nothing can fix that from here; it is a property of the interface, and it is why cron is the third choice rather than the first.

**Cron cannot express an arbitrary interval.** `*/7 * * * *` does not mean *every seven minutes*; it means minutes 0, 7, 14, 21, 28, 35, 42, 49, and 56 — a step that resets at the top of the hour, leaving a four-minute gap and then an eleven-minute one. Rounding the interval to something cron can say would be installing a timer that is not the one the operator asked for, so an interval cron cannot express makes this backend **unusable** and detection says so rather than installing something else.
"""

import shutil
from shlex import quote

from .entry import Runner, TimerEntry, TimerError, run_command, shell_command

NAME = "cron"

NO_CRONTAB = 1
"""What `crontab -l` exits with when the user has none. Not an error — it is the ordinary state of a machine nobody has scheduled anything on, and treating it as one would make cron unusable for exactly the people who need it."""


def markers(label: str) -> tuple[str, str]:
    """The comment pair Steward's block lives between.

    Visible and self-explaining, because the reader who meets them is an operator opening their own crontab and wondering what wrote in it.
    """
    return (f"# >>> {label} >>>", f"# <<< {label} <<<")


def cron_schedule(interval: int) -> str | None:
    """The five-field expression for an interval, where cron can say it.

    Args:
        interval: Seconds between tends.

    Returns:
        A cron expression, or `None` where cron cannot express this interval evenly — a sub-minute interval, one that is not a whole number of minutes, or a step that would not divide its field.
    """
    if interval < 60 or interval % 60:
        return None
    minutes = interval // 60
    if minutes < 60:
        # a step only lands evenly when it divides the field it steps through,
        # because the field restarts each hour
        return f"*/{minutes} * * * *" if 60 % minutes == 0 else None
    if minutes % 60:
        return None
    hours = minutes // 60
    if hours == 24:
        return "0 0 * * *"
    return f"0 */{hours} * * *" if 24 % hours == 0 else None


def cron_line(entry: TimerEntry) -> str:
    """The crontab entry itself.

    `cd` into the workspace first, because cron starts a job in the user's home and everything downstream — `Workspace.find`, a relative `log_dir`, and inspect's own `.env` search — resolves from the working directory.

    **Every `%` is escaped, and shell quoting is no help with it.** cron reads the command field itself before handing anything to a shell, and an unescaped `%` there becomes a newline: the command is truncated at that point and everything after it is fed to the job on stdin. `quote` cannot prevent that because cron gets there first, so the escaping is applied to the assembled command rather than to its parts. A `%` in a workspace path is unusual and entirely legal, and the failure it produced would be a timer that silently ran half a command.

    Args:
        entry: What to schedule.

    Returns:
        One crontab line.

    Raises:
        TimerError: Cron cannot express this interval.
    """
    schedule = cron_schedule(entry.interval)
    if schedule is None:
        raise TimerError(
            f"cron cannot run something every {entry.interval} seconds — it "
            f"steps within each hour, so an interval must divide 60 minutes or "
            f"24 hours evenly"
        )
    # the command field *is* a shell line, so it takes `shell_command` directly
    # rather than through an `sh -c` the other two backends have to add
    body = f"cd {quote(str(entry.workspace))} && {shell_command(entry)}"
    return f"{schedule} {body.replace('%', r'\%')}"


def without_block(crontab: str, label: str) -> str:
    """Everything in a crontab that is not this label's block.

    Args:
        crontab: The current crontab.
        label: Whose block to remove.

    Returns:
        The crontab with the block gone and every other line untouched.
    """
    opening, closing = markers(label)
    kept: list[str] = []
    inside = False
    for line in crontab.splitlines():
        if line.strip() == opening:
            inside = True
        elif line.strip() == closing:
            inside = False
        elif not inside:
            kept.append(line)
    return "\n".join(kept)


def with_block(crontab: str, entry: TimerEntry) -> str:
    """A crontab holding exactly one block for this entry.

    Replaces rather than appends, so re-arming at a new interval leaves one line rather than two that both fire.

    Args:
        crontab: The current crontab.
        entry: What to schedule.

    Returns:
        The crontab to install.

    Raises:
        TimerError: Cron cannot express this interval.
    """
    line = cron_line(entry)
    opening, closing = markers(entry.label)
    body = without_block(crontab, entry.label).rstrip("\n")
    block = f"{opening}\n{line}\n{closing}"
    return f"{body}\n{block}\n" if body else f"{block}\n"


class Cron:
    """The shared-crontab backend."""

    name = NAME

    def __init__(self, runner: Runner = run_command) -> None:
        self.runner = runner

    def usable(self, entry: TimerEntry) -> bool:
        # the binary first, because `run_command` raises rather than returning
        # when it is absent -- and absent is the ordinary state of a minimal
        # container, which must fall through to being told so rather than
        # escaping detection as an error (`Launchd` and `Systemd` guard the
        # same way, for the same reason)
        if shutil.which("crontab") is None:
            return False
        if cron_schedule(entry.interval) is None:
            return False
        return self._read() is not None

    def describe(self, entry: TimerEntry) -> str:
        return f"a crontab entry marked {entry.label}"

    def arm(self, entry: TimerEntry) -> None:
        current = self._require()
        self._install(with_block(current, entry))

    def disarm(self, entry: TimerEntry) -> None:
        current = self._require()
        updated = without_block(current, entry.label)
        if updated.strip() == current.strip():
            return
        self._install(updated if updated.strip() else "")

    def armed(self, entry: TimerEntry) -> bool:
        current = self._read()
        return current is not None and markers(entry.label)[0] in current

    def _read(self) -> str | None:
        """The user's crontab, or `None` where cron cannot be reached at all."""
        result = self.runner(["crontab", "-l"], None)
        if result.ok:
            return result.output + "\n" if result.output else ""
        # no crontab yet is an empty one. Anything else -- no crond, no
        # permission, a container without the binary -- means this backend
        # cannot be used, which is not the same as an empty schedule
        return "" if result.code == NO_CRONTAB else None

    def _require(self) -> str:
        current = self._read()
        if current is None:
            raise TimerError("this machine's crontab could not be read")
        return current

    def _install(self, crontab: str) -> None:
        result = self.runner(["crontab", "-"], crontab)
        if not result.ok:
            raise TimerError(
                f"crontab would not accept the new schedule: "
                f"{result.output or f'exit {result.code}'}"
            )


__all__ = [
    "NAME",
    "Cron",
    "cron_line",
    "cron_schedule",
    "markers",
    "with_block",
    "without_block",
]
