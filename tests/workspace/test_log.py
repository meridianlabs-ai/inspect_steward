"""`steward.log`, whose one hard requirement is that it never makes things worse.

Every caller here is already handling a failure. A logger that can raise a
second one turns a reported problem into an unreported crash, and the conditions
most worth recording — a full disk, a directory that went away — are exactly the
ones that would do it.
"""

from pathlib import Path

from inspect_steward._workspace import steward_log


def test_lines_accumulate_in_order(tmp_path: Path) -> None:
    log = tmp_path / "steward.log"

    steward_log(log, "first")
    steward_log(log, "second")

    lines = log.read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 1)[1] for line in lines] == ["first", "second"]
    # every instant Steward records is UTC with an explicit offset
    assert all(line.startswith("20") and line[:24].endswith("Z") for line in lines)


def test_the_directory_is_created_on_the_way(tmp_path: Path) -> None:
    log = tmp_path / "made" / "up" / "steward.log"

    steward_log(log, "a line")

    assert log.exists()


def test_a_traceback_stays_one_record(tmp_path: Path) -> None:
    log = tmp_path / "steward.log"

    steward_log(log, "could not spawn:\nTraceback\n  File x\nOSError: nope")

    # folded rather than truncated: the whole message is there, and a reader
    # counting lines is counting failures rather than stack frames
    assert log.read_text(encoding="utf-8").count("\n") == 1
    assert "OSError: nope" in log.read_text(encoding="utf-8")


def test_an_unwritable_destination_is_swallowed(tmp_path: Path) -> None:
    # the disk-full case, which is the one this file most exists for and the
    # one it can do least about
    blocked = tmp_path / "wall"
    blocked.write_text("not a directory", encoding="utf-8")

    steward_log(blocked / "steward.log", "a line nobody will read")


def test_a_read_only_workspace_is_swallowed(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        steward_log(locked / "steward.log", "a line nobody will read")
    finally:
        locked.chmod(0o700)
