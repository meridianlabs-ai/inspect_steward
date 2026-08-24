"""One writer at a time, for as long as a command runs.

"Steward is the single writer" is a claim about a *process*, and nothing else in the architecture makes that process unique: Steward detaches, it is restartable, and it is driven by a coding agent, which double-invokes far more readily than a human at a terminal does. A timer firing while an agent is mid-tend is the ordinary case rather than the edge.

**The mechanism is `fcntl.flock` on `.steward/claim`, and the reason is what happens when a holder dies.** Judging staleness by the claim file's age — the design's first answer — makes a crashed tend block its replacement until a timeout expires, and makes a wall clock's jumps into a correctness question. A kernel lock has neither problem: when the holder dies, by crash, Ctrl-C, or OOM kill, its descriptors close and the lock is simply gone, so the next `acquire` succeeds immediately with no age to compute. It also catches a double-acquire *within* one process, which an age rule does not.

A JSON payload is written inside the locked file, because the lock alone says *someone* holds it and a refusal has to say who. Release truncates it, so an unheld claim reads as empty rather than as its last holder.

**The payload is evidence, not authority**, and the distinction is what keeps a pid read off disk from being a pid killed off disk. Taking the lock and recording who took it are two operations, so for the instant between them the file still names the previous holder — which is a live pid to signal if that holder died and its pid was reused. Nothing about holding the lock proves the payload describes the holder. So `_signalable` re-establishes it before anything is killed, and its load-bearing test is that a process cannot have recorded a claim before it existed.

The one hazard the mechanism cannot defend against is the file being *unlinked* while a claim is held: the lock is attached to the inode, so the holder keeps it, the next acquire creates a fresh file and locks that, and two Stewards run at once. Nothing on the new file's side can see it, because the old inode is no longer reachable by name. `.steward/` is documented as disposable, so this is a real thing a person can do, and the answer is the same one the starting-worker case takes — delete it when nothing is running (workflow.md, *Three categories, and the one that matters*).

**What is left is a wedged holder**: alive, holding the lock, long past what a tend should take. `flock` cannot be taken from one without killing it, so that is what happens, by default and unattended. Killing a tend destroys no work — workers are detached and outlive it, and every write a tend makes is already built to be interrupted, because an interrupted tend is reconciled by the next one either way. The alternative, refusing and leaving a human to break it, is worth nothing at 2am, when there is no human and the entire point is that the run keeps converging.

`STALE_AFTER` is generous rather than tight: a healthy tend is seconds locally, but observing a few thousand logs in S3 is not, and the threshold has to clear the slowest *honest* tend rather than the typical one. Its two failure directions are asymmetric on purpose — a clock that moved backwards makes a holder look young and the tend refuses, while one that moved forwards makes it look old and costs a killed tend the next one reconciles.

**None of this is the correctness mechanism**, which is what keeps it this small. Correctness comes from `reconcile` being pure and from the in-flight record's intent-before-spawn; a claim broken wrongly costs a duplicate tend, and a duplicate tend is a no-op. What the claim buys is that two Stewards neither duplicate work nor interleave writes.

See execution.md, *What enforces single-writer*.
"""

import errno
import fcntl
import json
import os
import signal
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from socket import gethostname
from types import TracebackType
from typing import Any, cast

import psutil

from .._util.jsonl import utc_now

STALE_AFTER = timedelta(minutes=30)
"""How long a holder may hold before it is treated as wedged and killed."""

TERM_GRACE = 5.0
"""Seconds a holder is given to release after `SIGTERM`. A tend that handles it releases through its own `with`, which is the path this waits for."""

KILL_GRACE = 2.0
"""Seconds to wait for the kernel after `SIGKILL`, which the holder has no say in."""

_POLL = 0.02
_MAX_PAYLOAD = 8192


@dataclass(frozen=True)
class Held:
    """Someone else has it.

    A value rather than an exception: a held claim is an expected outcome that the caller computes on — it reports who has it — rather than an error anything should be surprised by.
    """

    pid: int | None
    """The holder, or `None` when the payload does not say."""

    host: str | None
    """Where it holds from. Always this machine in practice — the claim is a local file — and checked before anything is signalled, because a pid from elsewhere names an innocent process here."""

    command: str | None
    """The command holding it (`tend`, `launch`), for a message that says what is going on rather than only that something is."""

    since: str | None
    """When it was taken (UTC ISO-8601), or `None` when the payload is empty or torn."""

    stale: bool
    """Held past `stale_after`. Never true of a claim whose age cannot be read, which is what makes an unreadable payload safe."""

    unbroken: str | None = None
    """Why a break was attempted and this is still someone else's. `None` when none was attempted."""

    @property
    def age(self) -> timedelta | None:
        """How long it has been held, or `None` when the payload does not say."""
        return _age(self.since)


class Claim:
    """An exclusive hold on a workspace, released when the block exits.

    Released on the way out of a `with` whether the body succeeded or raised, which is the case that matters: a tend that dies holding the claim must not leave the next one locked out. The kernel covers the harder version of that — a process that dies without unwinding at all — so this only has to cover the ordinary one.
    """

    def __init__(self, path: Path, fd: int, *, broke: Held | None) -> None:
        self.path = path
        self.broke = broke
        """What this claim killed to get here, for the caller to journal. `None` in the ordinary case, where it was simply free."""
        self._fd: int | None = fd

    def __enter__(self) -> "Claim":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def release(self) -> None:
        """Give up the claim. Idempotent, so `with` and an explicit release do not fight."""
        if (fd := self._fd) is None:
            return
        self._fd = None
        try:
            # truncated while the lock is still held, so no reader can ever see
            # a payload that outlived its holder
            os.ftruncate(fd, 0)
        finally:
            os.close(fd)


def acquire(
    path: Path,
    *,
    command: str,
    break_stale: bool = True,
    stale_after: timedelta = STALE_AFTER,
) -> Claim | Held:
    """Take the run claim, or report who has it.

    Args:
        path: Path to `.steward/claim`. Created, along with its directory, if absent.
        command: The command taking it, recorded for whoever is refused later.
        break_stale: Kill a wedged holder and take the claim from it. On by default because an unattended run has nobody to ask; the caller turns it off when someone is attached and would rather look at the wedge than clear it.
        stale_after: How long counts as wedged. A parameter so a workspace can set it (`_steward.md`) and so tests can reach the break path without waiting.

    Returns:
        A `Claim` to hold for the duration of the work, or a `Held` describing the holder that would not give it up.

    Raises:
        OSError: If the claim file cannot be opened. A claim that cannot be taken is an answer; one that cannot be attempted is not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        outcome = _acquire(
            fd, path, command=command, break_stale=break_stale, stale_after=stale_after
        )
    except BaseException:
        os.close(fd)
        raise
    if isinstance(outcome, Held):
        os.close(fd)
    return outcome


def read_claim(path: Path, *, stale_after: timedelta = STALE_AFTER) -> Held | None:
    """Who holds a claim, for a command that does not want one.

    Answers from the lock rather than from the payload, so a holder that crashed reads as gone rather than as forever-running. The cost is a shared lock taken and dropped in consecutive statements, which a concurrent `acquire` could collide with; that costs one refused tend and the next one fixes it, where trusting the payload would misreport a crash every time until something overwrote it.

    Args:
        path: Path to `.steward/claim`.
        stale_after: How long counts as wedged.

    Returns:
        The holder, or `None` if nobody holds it.

    Raises:
        OSError: If the file exists but cannot be opened.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        if _lock(fd, fcntl.LOCK_SH):
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        return _held(fd, stale_after=stale_after)
    finally:
        os.close(fd)


def _acquire(
    fd: int,
    path: Path,
    *,
    command: str,
    break_stale: bool,
    stale_after: timedelta,
) -> Claim | Held:
    """`acquire` with the descriptor's lifetime handled by the caller."""
    if _lock(fd):
        _write(fd, command=command)
        return Claim(path, fd, broke=None)

    held = _held(fd, stale_after=stale_after)
    if not (break_stale and held.stale):
        return held

    if (failure := _break(fd, held, stale_after=stale_after)) is not None:
        # re-read rather than reporting `held`: the reason we do not have it
        # may be that another Steward broke the same wedge a moment sooner
        return replace(_held(fd, stale_after=stale_after), unbroken=failure)

    _write(fd, command=command)
    return Claim(path, fd, broke=held)


def _break(fd: int, held: Held, *, stale_after: timedelta) -> str | None:
    """Take the lock from a wedged holder, or say why it could not be taken.

    Success is judged by the lock rather than by the pid, and the difference is not academic: a killed holder that nothing has reaped yet is still a pid answering `kill(pid, 0)`, while the descriptors it held — and the lock with them — went at exit.

    **Every signal is preceded by its own check, not just the first.** Two tends breaking the same wedge is an ordinary race — a timer and an agent both finding it — and the one that loses the freed lock sits out the whole of `TERM_GRACE` before it would escalate. By then its notes are seconds old: the holder is dead, the claim belongs to somebody else, and the pid it remembers may have been reused. Re-reading narrows the exposure from seconds to the microseconds between the check and the `kill`, which no portable POSIX call closes entirely. It is also what lets the loser stop at once instead of spending `KILL_GRACE` on a signal that can no longer mean anything.
    """
    for sig, grace in ((signal.SIGTERM, TERM_GRACE), (signal.SIGKILL, KILL_GRACE)):
        target = _still_wedged(fd, held, stale_after=stale_after)
        if isinstance(target, str):
            return target
        try:
            os.kill(target, sig)
        except ProcessLookupError:
            # already gone, and the lock may already be free -- the wait below
            # is what decides, so there is nothing to report yet
            pass
        except OSError as ex:
            return f"could not signal pid {target}: {ex}"
        if _wait_lock(fd, grace):
            return None

    return f"pid {held.pid} did not release the claim"


def _still_wedged(fd: int, held: Held, *, stale_after: timedelta) -> int | str:
    """The pid to signal, re-confirmed against the claim as it stands right now.

    The identity test is the pid and the instant together, because a successor writes both: a claim that has changed hands is one this break has already lost, whatever it decided a moment ago.
    """
    current = _held(fd, stale_after=stale_after)
    if (current.pid, current.since) != (held.pid, held.since):
        return "the claim changed hands before the signal could land"
    return _signalable(current)


def _signalable(held: Held) -> int | str:
    """The pid to kill, or why the claim does not safely name one.

    Everything here guards one irreversible act against a payload that is only bytes on disk, and the third check is the one that is not obvious. **Taking the lock and recording who took it are two operations**, so for the instant between them the file still names the *previous* holder — and if that holder died and its pid has since been reused, a contender arriving in that window reads a live pid belonging to somebody else entirely. A process cannot have recorded a claim before it existed, so a start time later than the claim's own instant means the pid was reused and is not the holder. That is also the general answer to pid recycling, of which the window above is only the narrowest case.

    The instant is recorded to the millisecond and truncated, which moves it *earlier* and so makes this test stricter rather than looser. A real holder starts an interpreter and imports before it claims anything, which puts a comfortable second between the two.
    """
    if held.pid is None:
        return "the claim does not name a process"
    here = gethostname()
    if held.host != here:
        # the workspace is local, so this should not arise; if it does, a pid
        # from another machine names some innocent process on this one
        return f"the claim names a process on {held.host or 'no host at all'}, and this is {here}"
    if (at := _at(held.since)) is None:
        return "the claim does not say when it was taken"
    try:
        started = psutil.Process(held.pid).create_time()
    except psutil.Error:
        # nothing to kill, and yet the claim is held -- so it is held by
        # somebody who has not written their payload yet, and is not ours
        return f"pid {held.pid} is gone, and the claim is still held"
    if started > at.timestamp():
        return (
            f"pid {held.pid} started after the claim was taken, so it is not the holder"
        )
    return held.pid


def _lock(fd: int, operation: int = fcntl.LOCK_EX) -> bool:
    """Try for the lock without waiting. `True` if this descriptor now holds it."""
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
        return True
    except OSError as ex:
        if ex.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return False
        raise


def _wait_lock(fd: int, grace: float) -> bool:
    """Poll for the lock until a deadline, holding it if it comes free."""
    deadline = time.monotonic() + grace
    while True:
        if _lock(fd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


def _write(fd: int, *, command: str) -> None:
    """Record who holds the claim, into the file the lock is on.

    Not flushed to disk. The only readers are other processes on this machine, which see it through the page cache, and a payload that survived a power cut would name a process that did not.
    """
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "host": gethostname(),
            "command": command,
            "since": utc_now(),
        }
    )
    os.ftruncate(fd, 0)
    os.pwrite(fd, payload.encode("utf-8"), 0)


def _held(fd: int, *, stale_after: timedelta) -> Held:
    """Describe the holder from the payload, which may say nothing."""
    payload = _payload(fd)
    since = _str(payload.get("since"))
    age = _age(since)
    return Held(
        pid=_pid(payload.get("pid")),
        host=_str(payload.get("host")),
        command=_str(payload.get("command")),
        since=since,
        stale=age is not None and age > stale_after,
    )


def _payload(fd: int) -> dict[str, Any]:
    """The claim document, or an empty one when there is nothing readable there."""
    try:
        parsed: Any = json.loads(os.pread(fd, _MAX_PAYLOAD, 0))
    except ValueError:
        # an empty file, from a holder that crashed between locking and
        # writing, or a fragment, from one that crashed during it. Both mean
        # held-by-someone-unknown, which is a report rather than damage
        return {}
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}


def _at(since: str | None) -> datetime | None:
    """An instant the payload recorded, or `None` when it is not one."""
    if since is None:
        return None
    try:
        at = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return None
    # every instant Steward records carries an offset (execution.md, *Clocks*),
    # so a naive one is not one of ours to interpret
    return at if at.tzinfo is not None else None


def _age(since: str | None) -> timedelta | None:
    """How long ago an instant was, by the local wall clock.

    Clamped at zero, so the one direction a clock jump can push this is toward *not* stale, and therefore toward refusing rather than toward killing.
    """
    at = _at(since)
    return max(datetime.now(timezone.utc) - at, timedelta(0)) if at else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _pid(value: Any) -> int | None:
    """A pid from the payload, or `None` when it is not one.

    Non-positive values are dropped rather than carried: `os.kill(0, sig)` signals the caller's *own* process group and a negative pid signals somebody else's, so a corrupt byte in this field is the difference between killing one wedged tend and killing everything around it.
    """
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )
