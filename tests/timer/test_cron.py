"""Editing a file that belongs to everybody.

`crontab -` replaces the whole file, so there is no *add my line* primitive and
Steward's arming is a read-modify-write. That makes one property load-bearing
above all others: **every line outside Steward's markers survives byte for
byte**. A backend that mangles somebody's nightly backup while installing a
tend timer is worse than no cron backend at all.

The other half is the interval. Cron steps within each hour rather than from
now, so `*/7` is not *every seven minutes* — and installing a rounded interval
would be installing a timer nobody asked for.
"""

from pathlib import Path

import pytest
from inspect_steward._timer import (
    Cron,
    TimerEntry,
    cron_line,
    cron_schedule,
    markers,
    timer_entry,
    with_block,
    without_block,
)

from ._fake import FakeRunner, crontab_present, fails, succeeds

WORKSPACE = Path("/tmp/sweeps/overnight").resolve()

THEIRS = "\n".join(
    [
        "MAILTO=ops@example.com",
        "# nightly backup",
        "0 3 * * * /usr/local/bin/backup --full",
        "*/5 * * * * /usr/local/bin/heartbeat",
    ]
)
"""Somebody else's crontab. Includes an environment assignment and a comment, because a naive line filter mangles both."""


@pytest.fixture(autouse=True)
def has_crontab(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine with cron installed, whichever machine is running the suite."""
    crontab_present(monkeypatch)


def entry(interval: int = 600, root: Path = WORKSPACE) -> TimerEntry:
    return timer_entry(root, interval, output=root / ".steward" / "timer.log")


def theirs(crontab: str) -> list[str]:
    """The lines in a crontab that are not Steward's, for comparing before and after."""
    opening, closing = markers(entry().label)
    kept: list[str] = []
    inside = False
    for line in crontab.splitlines():
        if line.strip() == opening:
            inside = True
        elif line.strip() == closing:
            inside = False
        elif not inside and line.strip():
            kept.append(line)
    return kept


# --- the block edit -----------------------------------------------------


def test_everything_outside_the_markers_survives() -> None:
    updated = with_block(THEIRS + "\n", entry())

    assert theirs(updated) == THEIRS.splitlines()


def test_arming_twice_leaves_one_block() -> None:
    # the ordinary second reason to arm is a new interval, and two blocks would
    # both fire
    once = with_block(THEIRS + "\n", entry(600))
    twice = with_block(once, entry(1800))

    opening, _ = markers(entry().label)
    assert twice.count(opening) == 1
    assert "*/30 * * * *" in twice
    assert "*/10 * * * *" not in twice
    assert theirs(twice) == THEIRS.splitlines()


def test_disarming_removes_exactly_the_block() -> None:
    original = THEIRS + "\n"
    armed = with_block(original, entry())

    disarmed = without_block(armed, entry().label)

    assert theirs(disarmed) == THEIRS.splitlines()
    assert markers(entry().label)[0] not in disarmed


def test_disarming_a_crontab_that_was_only_ours_leaves_nothing() -> None:
    armed = with_block("", entry())

    assert without_block(armed, entry().label).strip() == ""


def test_another_workspace_s_block_is_not_touched() -> None:
    # two workspaces on one machine, each with its own timer -- disarming one
    # must not disarm the other, which is the whole reason the label is derived
    # from the path
    other = entry(600, Path("/tmp/sweeps/other").resolve())
    both = with_block(with_block("", entry()), other)

    remaining = without_block(both, entry().label)

    assert markers(other.label)[0] in remaining
    assert markers(entry().label)[0] not in remaining


def test_a_crontab_with_no_trailing_newline_is_not_joined_to_our_block() -> None:
    # cron ignores a file whose last line has no newline, and the join would
    # make somebody's heartbeat entry the tail of our marker comment
    updated = with_block("0 3 * * * /usr/local/bin/backup", entry())

    assert "/usr/local/bin/backup\n" in updated
    assert updated.endswith("\n")
    assert theirs(updated) == ["0 3 * * * /usr/local/bin/backup"]


# --- what cron can and cannot say ---------------------------------------


SCHEDULES: list[tuple[str, int, str | None]] = [
    ("ten minutes", 600, "*/10 * * * *"),
    ("one minute", 60, "*/1 * * * *"),
    ("thirty minutes", 1800, "*/30 * * * *"),
    ("an hour", 3600, "0 */1 * * *"),
    ("six hours", 21600, "0 */6 * * *"),
    ("a day", 86400, "0 0 * * *"),
    # a step that does not divide its field restarts at the top of the hour,
    # leaving a four-minute gap and then an eleven-minute one
    ("seven minutes", 420, None),
    ("forty minutes", 2400, None),
    ("five hours", 18000, None),
    ("ninety seconds", 90, None),
    ("thirty seconds", 30, None),
]


@pytest.mark.parametrize(
    ("interval", "expected"),
    [(interval, expected) for _, interval, expected in SCHEDULES],
    ids=[case for case, _, _ in SCHEDULES],
)
def test_an_interval_is_expressed_evenly_or_not_at_all(
    interval: int, expected: str | None
) -> None:
    assert cron_schedule(interval) == expected


def test_a_percent_sign_in_a_path_is_escaped(tmp_path: Path) -> None:
    """Cron reads the command field before any shell does, and `%` there is a newline.

    Everything after the first unescaped one is truncated off the command and
    fed to it on stdin, so a workspace path with a `%` in it would install a
    timer that silently ran half a command. `shlex.quote` cannot prevent it,
    which is why the escaping is applied to the assembled line.
    """
    odd = tmp_path / "sweep-100%-coverage"
    odd.mkdir()
    line = cron_line(entry(600, root=odd))

    assert r"100\%-coverage" in line
    # the schedule is the only place a bare one may appear, and it never has one
    assert "%" not in line.replace(r"\%", "")[len("*/10 * * * *") :]


def test_an_interval_cron_cannot_express_makes_cron_unusable() -> None:
    # declining is the right answer; rounding would install a timer the operator
    # did not ask for, and there is no fourth backend to quietly absorb it
    runner = FakeRunner()

    assert Cron(runner).usable(entry(600))
    assert not Cron(runner).usable(entry(420))


# --- reaching the system ------------------------------------------------


def test_no_crontab_yet_is_an_empty_one_rather_than_a_refusal() -> None:
    # the ordinary state of a machine nobody has scheduled anything on, and
    # treating it as an error would make cron unusable for exactly those people
    runner = FakeRunner({"crontab -l": fails("crontab: no crontab for jj", code=1)})

    Cron(runner).arm(entry())

    assert runner.stdin[-1] is not None
    assert markers(entry().label)[0] in runner.stdin[-1]


def test_a_cron_that_cannot_be_reached_at_all_is_not_usable() -> None:
    # no crond, no permission, or a container without the binary -- none of the
    # three is an empty schedule
    runner = FakeRunner({"crontab -l": fails("command not found", code=127)})

    assert not Cron(runner).usable(entry())


def test_arming_installs_through_stdin_and_keeps_what_was_there() -> None:
    runner = FakeRunner({"crontab -l": succeeds(THEIRS)})

    Cron(runner).arm(entry())

    assert runner.asked("crontab", "-")
    installed = runner.stdin[-1]
    assert installed is not None
    assert theirs(installed) == THEIRS.splitlines()
    assert "*/10 * * * *" in installed


def test_disarming_with_nothing_of_ours_installed_writes_nothing() -> None:
    # a crontab rewrite is a race with anybody else editing it, so the safest
    # rewrite is the one that does not happen
    runner = FakeRunner({"crontab -l": succeeds(THEIRS)})

    Cron(runner).disarm(entry())

    assert runner.commands == ["crontab -l"]
