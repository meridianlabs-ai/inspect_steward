"""What each backend installs, as a pure function of an entry.

The half of a scheduler that can be tested honestly. Rendering a plist, a
systemd unit, or a crontab line touches nothing, so these cases are exhaustive
where the activation cases can only assert an argv.

What matters in all three is the same three facts — run *this*, in *that*
directory, every *so often* — because every way a timer can be silently wrong
is one of them being lost in translation.
"""

import plistlib
import shlex
import sys
from pathlib import Path
from typing import Any

from inspect_steward._timer import (
    TimerEntry,
    cron_line,
    entry_label,
    render_plist,
    render_service,
    render_timer,
    timer_entry,
)

WORKSPACE = Path("/tmp/sweeps/overnight").resolve()
"""Resolved, because `timer_entry` resolves — and it must, or arming from inside the
workspace and arming from beside it would install two entries for one run."""


def entry(interval: int = 600, root: Path = WORKSPACE) -> TimerEntry:
    return timer_entry(root, interval, output=root / ".steward" / "timer.log")


def plist(interval: int = 600) -> dict[str, Any]:
    loaded: Any = plistlib.loads(render_plist(entry(interval)))
    return loaded


# --- what every backend has to carry ------------------------------------


def test_the_command_is_an_absolute_interpreter_and_a_module() -> None:
    # a scheduled command inherits almost no PATH, so a bare `steward` or a
    # bare `python` is either absent or the wrong one
    argv = entry().argv

    assert argv[0] == sys.executable
    assert Path(argv[0]).is_absolute()
    assert argv[1:] == ["-m", "inspect_steward", "tend"]


def test_launchd_carries_the_interval_the_command_and_the_directory() -> None:
    rendered = plist(900)

    assert rendered["StartInterval"] == 900
    assert rendered["WorkingDirectory"] == str(WORKSPACE)
    assert rendered["Label"] == entry_label(WORKSPACE)
    # the tend reaches launchd through a shell, because launchd will not create
    # the output's directory and `.steward/` may have been deleted
    program, flag, command = rendered["ProgramArguments"]
    assert (program, flag) == ("/bin/sh", "-c")
    assert " ".join(entry().argv) in command
    assert "StandardOutPath" not in rendered


def test_launchd_does_not_tend_the_moment_it_is_armed() -> None:
    # `launch` has just converged the run; a second turn a millisecond later
    # would find the claim held and refuse, which is a confusing first line in
    # a timer log for no gain
    assert plist()["RunAtLoad"] is False


def test_systemd_carries_the_interval_the_command_and_the_directory() -> None:
    service, timer = render_service(entry(900)), render_timer(entry(900))

    assert "OnUnitActiveSec=900s" in timer
    assert "OnBootSec=900s" in timer
    assert f"Unit={entry_label(WORKSPACE)}.service" in timer
    (execstart,) = [
        line for line in service.splitlines() if line.startswith("ExecStart=")
    ]
    program, flag, command = shlex.split(execstart[len("ExecStart=") :])
    assert (program, flag) == ("/bin/sh", "-c")
    assert " ".join(entry().argv) in command
    assert "StandardOutput=" not in service
    assert f"WorkingDirectory={WORKSPACE}" in service
    assert "Type=oneshot" in service


def test_cron_carries_the_interval_the_command_and_the_directory() -> None:
    line = cron_line(entry(900))

    schedule, rest = line.split(" ", 5)[:5], line.split(" ", 5)[5]
    assert schedule == ["*/15", "*", "*", "*", "*"]
    assert rest.startswith(f"cd {WORKSPACE} && ")
    assert " ".join(entry().argv) in rest
    assert rest.endswith(f"{WORKSPACE / '.steward' / 'timer.log'} 2>&1")


# --- the label, which is what makes disarming precise -------------------


def test_two_workspaces_get_two_labels() -> None:
    # three sweeps called `overnight` on one machine is the ordinary case, and
    # a shared label would make arming the second silently replace the first
    assert entry_label(Path("/tmp/a/overnight")) != entry_label(
        Path("/tmp/b/overnight")
    )


def test_a_label_is_stable_and_says_what_wrote_it() -> None:
    label = entry_label(WORKSPACE)

    assert label == entry_label(WORKSPACE)
    assert label.startswith("steward-")
    # has to survive being a filename, a systemd unit name, and a launchd
    # domain target at once
    assert label.replace("-", "").isalnum()


def test_a_relative_path_labels_the_same_as_its_absolute_form(tmp_path: Path) -> None:
    # arming from inside the workspace and arming from beside it are the same
    # workspace, and a scheduler that thought otherwise would hold two entries
    (tmp_path / "run").mkdir()
    assert entry_label(tmp_path / "run") == entry_label(tmp_path / "run" / "." / "")


# --- paths with characters a template would break -----------------------


AWKWARD = Path("/tmp/sweeps/it's & mine/run one").resolve()
"""An apostrophe, an ampersand, and a space — a real directory to have, and one that each backend has to escape for a different grammar."""


def test_the_plist_stays_a_plist_when_the_path_has_an_ampersand() -> None:
    # the case a hand-written XML template gets wrong, and launchd's complaint
    # about it names a line number rather than the path
    rendered: Any = plistlib.loads(render_plist(entry(600, AWKWARD)))

    assert rendered["WorkingDirectory"] == str(AWKWARD)
    # split, because the path is shell-quoted in the command and an apostrophe
    # in it is exactly what the quoting is for
    assert str(AWKWARD / ".steward" / "timer.log") in shlex.split(
        rendered["ProgramArguments"][2]
    )


def test_the_crontab_line_survives_a_shell() -> None:
    # cron hands its line to /bin/sh, so an unquoted apostrophe is an unclosed
    # string and the job never runs -- with cron's only report being a mail
    # nobody on a headless box reads
    line = cron_line(entry(600, AWKWARD))
    _, command = line.split(" ", 5)[5].split("cd ", 1)
    target, rest = shlex.split(command)[0], shlex.split(command)

    assert target == str(AWKWARD)
    assert str(AWKWARD / ".steward" / "timer.log") in rest


def test_the_systemd_execstart_survives_a_path_with_a_space() -> None:
    # systemd splits ExecStart on whitespace, so an interpreter under a home
    # with a space in it needs quoting
    awkward = TimerEntry(
        workspace=AWKWARD,
        interval=600,
        label="steward-test",
        argv=["/opt/py 3.13/bin/python", "-m", "inspect_steward", "tend"],
        output=AWKWARD / "timer.log",
    )

    (execstart,) = [
        line
        for line in render_service(awkward).splitlines()
        if line.startswith("ExecStart=")
    ]

    # `sh -c` first, then the command it is handed, which is what has to keep
    # the interpreter in one piece
    _, _, command = shlex.split(execstart[len("ExecStart=") :])
    assert shlex.split(command)[shlex.split(command).index("exec") + 1] == (
        "/opt/py 3.13/bin/python"
    )
