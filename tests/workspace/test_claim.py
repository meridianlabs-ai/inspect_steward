"""The run claim, and mostly what it does to a holder that will not let go.

Most of this needs no second process, because `flock` refuses a second
descriptor in the *same* process: the double-invoke a coding agent produces is
caught by the same mechanism as the double-invoke a timer produces, and both
are reachable from one test. What does need one is breaking — the single
process a break must never kill is the caller.

The wedge is manufactured by shrinking `stale_after` rather than by backdating
a payload, so the break path runs against a live process and a real clock.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from socket import gethostname

import pytest
from inspect_steward._workspace import Claim, Held, acquire, read_claim, utc_now
from inspect_steward._workspace import claim as claim_module

WEDGED = timedelta(milliseconds=10)
"""A threshold any live holder crosses at once."""

HOLDER = """
import signal
import sys
import time
from pathlib import Path

from inspect_steward._workspace import Claim, acquire

path, command, ready, ears = sys.argv[1:5]
if ears == "deaf":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

claim = acquire(Path(path), command=command)
assert isinstance(claim, Claim), claim
Path(ready).touch()
time.sleep(600)
"""


HANDS_OVER = """
import os
import signal
import sys
import time

path, successor, ready = sys.argv[1:4]

def hand_over(signum, frame):
    # what a real holder's successor writes, compressed into the moment the
    # breaker is waiting out its grace period
    fd = os.open(path, os.O_WRONLY)
    os.ftruncate(fd, 0)
    os.pwrite(fd, successor.encode("utf-8"), 0)
    os.close(fd)
    sys.exit(0)

signal.signal(signal.SIGTERM, hand_over)
open(ready, "w").close()
time.sleep(600)
"""

Children = list["subprocess.Popen[str]"]


@pytest.fixture
def children() -> Iterator[Children]:
    """Child processes, killed however the test ends."""
    started: Children = []
    yield started
    for child in started:
        child.kill()
        child.wait(timeout=30)


def idle(children: Children) -> "subprocess.Popen[str]":
    """Start a process that does nothing, for a payload to point at.

    A bare interpreter rather than a claim holder, and 50ms rather than the 1.3s importing Steward costs: what these are for is being a live pid, and what they prove is that a live pid is not enough.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"], text=True
    )
    children.append(child)
    return child


def hold(
    children: Children,
    path: Path,
    *,
    command: str = "tend",
    deaf: bool = False,
) -> "subprocess.Popen[str]":
    """Start a child holding the claim, and return once it does."""
    ready = path.parent / f"ready-{len(children)}"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            HOLDER,
            str(path),
            command,
            str(ready),
            "deaf" if deaf else "hears",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    children.append(child)
    deadline = time.monotonic() + 60
    while not ready.exists():
        if child.poll() is not None:
            raise AssertionError(f"the holder exited: {child.communicate()[0]}")
        assert time.monotonic() < deadline, "the holder never took the claim"
        time.sleep(0.02)
    # a holder is wedged by having held for a while, and `utc_now` has
    # millisecond resolution -- so put unambiguous time between the two
    time.sleep(0.05)
    return child


@contextmanager
def locked(path: Path, payload: bytes) -> Generator[None]:
    """Hold the claim on a raw descriptor, with a payload no writer would produce."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.pwrite(fd, payload, 0)
        yield
    finally:
        os.close(fd)


# --- one process at a time ----------------------------------------------


def test_a_second_acquire_is_refused(tmp_path: Path) -> None:
    path = tmp_path / ".steward" / "claim"
    first = acquire(path, command="tend")
    assert isinstance(first, Claim)

    try:
        second = acquire(path, command="launch")
        assert isinstance(second, Held)
        # a refusal has to say who, which is the whole reason the lock carries
        # a payload rather than standing alone
        assert second.pid == os.getpid()
        assert second.host == gethostname()
        assert second.command == "tend"
        assert second.since is not None and second.since.endswith("Z")
        assert not second.stale and second.unbroken is None
    finally:
        first.release()


def test_release_lets_the_next_one_in(tmp_path: Path) -> None:
    path = tmp_path / ".steward" / "claim"
    first = acquire(path, command="tend")
    assert isinstance(first, Claim)

    first.release()
    first.release()

    # nothing of the last holder survives, which is what makes the one pid a
    # break ever reads off disk a live one
    assert path.read_bytes() == b""
    second = acquire(path, command="tend")
    assert isinstance(second, Claim)
    second.release()


def test_a_claim_is_released_when_the_block_raises(tmp_path: Path) -> None:
    path = tmp_path / ".steward" / "claim"
    first = acquire(path, command="tend")
    assert isinstance(first, Claim)

    with pytest.raises(RuntimeError):
        with first:
            raise RuntimeError("a tend that failed partway")

    after = acquire(path, command="tend")
    assert isinstance(after, Claim)
    after.release()


def test_read_claim_needs_no_lock(tmp_path: Path) -> None:
    path = tmp_path / ".steward" / "claim"
    assert read_claim(path) is None

    claim = acquire(path, command="tend")
    assert isinstance(claim, Claim)
    holder = read_claim(path)
    assert holder is not None
    assert holder.pid == os.getpid() and holder.command == "tend"

    claim.release()
    # answered from the lock rather than the payload, so a holder that crashed
    # reads as gone rather than as forever-running
    assert read_claim(path) is None


# --- a payload that cannot be trusted ------------------------------------

UNREADABLE = [
    ("nothing was written", b""),
    ("the write was cut short", b'{"pid": 4242, "comm'),
]


@pytest.mark.parametrize(
    "payload",
    [pytest.param(case[1], id=case[0].replace(" ", "_")) for case in UNREADABLE],
)
def test_an_unreadable_payload_is_never_stale(tmp_path: Path, payload: bytes) -> None:
    # a holder that crashed between locking and writing, or during it. Either
    # way the pid it might have named is the one thing not to act on, so no
    # threshold makes it breakable
    path = tmp_path / ".steward" / "claim"
    with locked(path, payload):
        held = acquire(path, command="tend", stale_after=WEDGED)

    assert isinstance(held, Held)
    assert held.pid is None and held.since is None and held.age is None
    assert not held.stale and held.unbroken is None


def test_a_clock_that_moved_backwards_refuses(tmp_path: Path) -> None:
    # a holder stamped in the future, which is what a clock correction leaves
    # behind. The age clamps at zero, so the only direction a jump can push
    # this is toward refusing rather than toward killing
    path = tmp_path / ".steward" / "claim"
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "host": gethostname(),
            "command": "tend",
            "since": "2099-01-01T00:00:00.000Z",
        }
    ).encode("utf-8")

    with locked(path, payload):
        held = acquire(path, command="tend", break_stale=False, stale_after=WEDGED)

    assert isinstance(held, Held)
    assert held.age == timedelta(0)
    assert not held.stale


# --- what the payload has to prove before anything is killed -------------

Edit = Callable[[int], dict[str, object]]
"""One field of an otherwise breakable payload, replaced."""

UNSIGNALABLE: list[tuple[str, Edit, str]] = [
    # `os.kill(0, sig)` signals the caller's own process group, and a negative
    # pid signals somebody else's -- so these two are the difference between
    # killing one wedged tend and killing everything around it
    ("a pid of zero", lambda pid: {"pid": 0}, "does not name a process"),
    ("a negative pid", lambda pid: {"pid": -pid}, "does not name a process"),
    ("no host at all", lambda pid: {"host": None}, "no host at all"),
    ("another machine's host", lambda pid: {"host": "elsewhere"}, "and this is"),
    # the lock and the payload are written by two separate operations, so a
    # contender arriving between them reads the *previous* holder -- which is
    # somebody else's live process once that pid has been reused
    (
        "a pid that started after the claim",
        lambda pid: {"since": "2020-01-01T00:00:00.000Z"},
        "started after the claim was taken",
    ),
]


@pytest.mark.parametrize(
    ("edit", "reason"),
    [pytest.param(*case[1:], id=case[0].replace(" ", "_")) for case in UNSIGNALABLE],
)
def test_a_payload_that_does_not_prove_its_holder_is_not_acted_on(
    tmp_path: Path,
    children: Children,
    edit: Edit,
    reason: str,
) -> None:
    """A live pid in a locked claim is still not licence to kill it.

    Each case is one field away from breakable — the lock is held, the age is
    past the threshold, and the pid is a real running process — so what the
    assertion pins is that the guard fired, and which one.
    """
    bystander = idle(children)
    time.sleep(0.05)
    payload: dict[str, object] = {
        "pid": bystander.pid,
        "host": gethostname(),
        "command": "tend",
        "since": utc_now(),
    }
    payload.update(edit(bystander.pid))
    time.sleep(0.05)

    path = tmp_path / ".steward" / "claim"
    with locked(path, json.dumps(payload).encode("utf-8")):
        held = acquire(path, command="tend", stale_after=WEDGED)

    assert isinstance(held, Held)
    assert held.unbroken is not None and reason in held.unbroken
    assert bystander.poll() is None


def test_a_claim_that_changed_hands_is_not_escalated_against(
    tmp_path: Path, children: Children, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tends breaking one wedge, from the losing side.

    A timer and an agent finding the same wedge is an ordinary race, and the
    one that loses the freed lock sits out the whole grace period before it
    would reach for `SIGKILL` — by which time the holder is dead, the claim
    belongs to the winner, and the pid it remembers is only a number. What is
    pinned here is the reason it gives, because that is where noticing shows
    up: the pid it *would* have signalled is the dead holder either way, so
    the harm this prevents (a reused pid) is not manufacturable on demand.
    """
    monkeypatch.setattr(claim_module, "TERM_GRACE", 0.3)
    path = tmp_path / ".steward" / "claim"
    winner = idle(children)
    successor = json.dumps(
        {
            "pid": winner.pid,
            "host": gethostname(),
            "command": "tend",
            "since": utc_now(),
        }
    )

    ready = tmp_path / "handed-over"
    wedged = subprocess.Popen(
        [sys.executable, "-c", HANDS_OVER, str(path), successor, str(ready)],
        text=True,
    )
    children.append(wedged)
    while not ready.exists():
        assert wedged.poll() is None, "the wedged holder exited before it held"
        time.sleep(0.02)
    time.sleep(0.05)

    payload = json.dumps(
        {
            "pid": wedged.pid,
            "host": gethostname(),
            "command": "tend",
            "since": utc_now(),
        }
    ).encode("utf-8")
    time.sleep(0.05)

    with locked(path, payload):
        held = acquire(path, command="tend", stale_after=WEDGED)

    assert isinstance(held, Held)
    assert held.unbroken is not None and "changed hands" in held.unbroken
    # and the refusal names the successor, not the holder it set out to break
    assert held.pid == winner.pid


def test_a_holder_that_is_already_gone_is_not_taken_over(
    tmp_path: Path, children: Children
) -> None:
    # the same window from the other side: the claim is held by somebody who
    # has not written a payload yet, and the pid on disk has since exited. The
    # right answer is to refuse, not to guess
    departed = idle(children)
    time.sleep(0.05)
    payload = json.dumps(
        {
            "pid": departed.pid,
            "host": gethostname(),
            "command": "tend",
            "since": utc_now(),
        }
    ).encode("utf-8")
    departed.kill()
    departed.wait(timeout=30)
    time.sleep(0.05)

    path = tmp_path / ".steward" / "claim"
    with locked(path, payload):
        held = acquire(path, command="tend", stale_after=WEDGED)

    assert isinstance(held, Held)
    assert held.unbroken is not None and "is gone" in held.unbroken


# --- holders in other processes ------------------------------------------


def test_a_crashed_holder_leaves_nothing_to_reap(
    tmp_path: Path, children: Children
) -> None:
    path = tmp_path / ".steward" / "claim"
    child = hold(children, path)
    child.kill()
    child.wait(timeout=30)

    started = time.monotonic()
    claim = acquire(path, command="tend", stale_after=timedelta(hours=1))
    elapsed = time.monotonic() - started

    # the payload is still on disk -- a SIGKILL leaves no chance to truncate --
    # so an age rule with this threshold would refuse for an hour. The lock is
    # the kernel's, and it went with the process
    assert isinstance(claim, Claim)
    assert claim.broke is None
    assert elapsed < 1
    claim.release()


def test_a_fresh_holder_is_not_broken(tmp_path: Path, children: Children) -> None:
    path = tmp_path / ".steward" / "claim"
    child = hold(children, path)

    # the default threshold, which is what a concurrent tend meets
    held = acquire(path, command="tend")

    assert isinstance(held, Held)
    assert held.pid == child.pid and not held.stale
    assert child.poll() is None


def test_a_wedged_holder_is_broken(tmp_path: Path, children: Children) -> None:
    path = tmp_path / ".steward" / "claim"
    child = hold(children, path, command="tend")

    claim = acquire(path, command="tend", stale_after=WEDGED)

    assert isinstance(claim, Claim)
    # what it killed, for the caller to journal -- nothing else records it
    assert claim.broke is not None
    assert claim.broke.pid == child.pid and claim.broke.stale
    assert child.wait(timeout=30) == -signal.SIGTERM

    holder = read_claim(path)
    assert holder is not None and holder.pid == os.getpid()
    claim.release()


def test_a_wedge_is_left_alone_when_asked(tmp_path: Path, children: Children) -> None:
    path = tmp_path / ".steward" / "claim"
    child = hold(children, path)

    held = acquire(path, command="tend", break_stale=False, stale_after=WEDGED)

    assert isinstance(held, Held)
    assert held.stale and held.pid == child.pid
    # nothing was attempted, as against attempted and failed
    assert held.unbroken is None
    assert child.poll() is None


def test_a_holder_deaf_to_sigterm_is_killed_anyway(
    tmp_path: Path,
    children: Children,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the case that makes escalation necessary rather than theoretical: a
    # process wedged badly enough to hold the claim is not obviously one that
    # will act on a request to stop
    monkeypatch.setattr(claim_module, "TERM_GRACE", 0.2)
    path = tmp_path / ".steward" / "claim"
    child = hold(children, path, deaf=True)

    claim = acquire(path, command="tend", stale_after=WEDGED)

    assert isinstance(claim, Claim)
    assert child.wait(timeout=30) == -signal.SIGKILL
    claim.release()
