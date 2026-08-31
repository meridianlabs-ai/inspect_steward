"""Sweeping the process table, and the race every sweep of it loses.

A worker is found by a marker in its own environment — its selection path —
which means reading processes that may exit mid-read. What is asserted here is
the skip: a process that cannot be read is not a process of Steward's, and it
must not take the rest of the sweep down with it.

`SystemError` earns its own case because it is the one a reasonable author
leaves out. macOS surfaces a mid-read exit from `proc_environ` as a bare
`SystemError` rather than as a `psutil.Error`, so a sweep guarded on psutil's
own hierarchy fails intermittently, and fails more often the busier the machine
is.
"""

import os
from typing import Any

import psutil
import pytest
from inspect_steward._util.process import ProcessInfo, process_table


class Unreadable:
    """A process that raises when asked anything, as an exiting one does."""

    def __init__(self, pid: int, raises: BaseException) -> None:
        self.pid = pid
        self._raises = raises

    def ppid(self) -> int:
        raise self._raises

    def environ(self) -> dict[str, str]:
        raise self._raises


class Readable:
    def __init__(self, pid: int, environ: dict[str, str]) -> None:
        self.pid = pid
        self._environ = environ

    def ppid(self) -> int:
        return 1

    def environ(self) -> dict[str, str]:
        return dict(self._environ)


def table(monkeypatch: pytest.MonkeyPatch, *processes: Any) -> list[ProcessInfo]:
    monkeypatch.setattr(
        "inspect_steward._util.process.psutil.process_iter", lambda: iter(processes)
    )
    return list(process_table())


RACES: list[tuple[str, BaseException]] = [
    ("it exited", psutil.NoSuchProcess(1)),
    ("it is another user's", psutil.AccessDenied(1)),
    ("it became a zombie", psutil.ZombieProcess(1)),
    # the one a psutil-only guard misses: macOS `proc_environ` against a
    # process that exits mid-read returns with an exception already set
    (
        "proc_environ raced its exit",
        SystemError("returned a result with an exception set"),
    ),
    ("the kernel would not answer", OSError("interrupted")),
]


@pytest.mark.parametrize(
    "raises",
    [raises for _, raises in RACES],
    ids=[case for case, _ in RACES],
)
def test_a_process_that_cannot_be_read_is_skipped_rather_than_fatal(
    raises: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the sweep runs against every process on the machine, so meeting one of
    # these is the ordinary case rather than the edge -- and a sweep that
    # raised would take out a whole tend
    found = table(
        monkeypatch,
        Unreadable(101, raises),
        Readable(102, {"STEWARD_TICKER": "/tmp/run"}),
    )

    assert [entry.pid for entry in found] == [102]


def test_an_unreadable_process_does_not_hide_the_ones_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = table(
        monkeypatch,
        Readable(101, {"A": "1"}),
        Unreadable(102, SystemError("boom")),
        Readable(103, {"B": "2"}),
    )

    assert [entry.pid for entry in found] == [101, 103]


def test_a_consumer_s_own_failure_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the yield sits outside the guard.

    A generator suspended inside a `try` catches whatever is thrown in at the
    yield, so the obvious spelling would silently turn a caller's own
    `psutil.Error` into one more skipped row — a real failure disappearing into
    the mechanism built to tolerate a different one.
    """
    monkeypatch.setattr(
        "inspect_steward._util.process.psutil.process_iter",
        lambda: iter([Readable(101, {"A": "1"})]),
    )

    with pytest.raises(psutil.AccessDenied):
        for _ in process_table():
            raise psutil.AccessDenied(101)


def test_the_real_table_holds_this_process() -> None:
    # one unfaked pass, so the fakes above are held to the shape psutil really
    # returns rather than to the shape this file imagines. Asserted on the
    # presence of a variable rather than its value: the environment a process
    # reports is the one it was exec'd with, which a later `setenv` in this
    # process does not change
    (mine,) = [entry for entry in process_table() if entry.pid == os.getpid()]

    assert mine.ppid == os.getppid()
    assert "PATH" in mine.environ
