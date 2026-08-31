"""`.steward/steward.log` — whether the machinery worked, as opposed to what it found.

The journal answers *was this eval set conducted properly*. A tend that crashed, a spawn that failed, a claim found wedged and killed, a `_steward.yaml` that would not parse — none of those is a fact about the eval. So the rule is a single question: **is this a fact about the eval set, or about Steward?** (workflow.md, *`steward.log` — whether Steward itself worked*.)

**Why a second file, stated as the code actually behaves.** Every caller here is a failure path, so a healthy run writes nothing at all — the "machinery is high-volume" argument is false in the ordinary case and the split does not rest on it. What it rests on is that **this file can be truncated and `journal.jsonl` cannot**. The case that matters is a *deterministic* failure: a wedged claim broken every turn, an unwritable `status.md`, a bucket that went away. That is a line per turn forever, which is a bound away from a problem here and unbounded growth of the one record a workspace cannot rebuild if it went there. Two lifetimes, two files.

**The journal carries the same text, deliberately, and the two are not redundant.** `_failed()` appends to the turn's `failures`, which the observation event records, *and* writes here. The journal's copy answers *what went wrong during this turn* — folded, rendered in history, diffed against the next turn. This one is the trail across turns, which is the only place a failure repeating identically at 02:10, 02:20 and 02:30 reads as a repetition rather than as three unrelated turns each having a bad night.

**It never raises, and that is the property that could not be added later.** The conditions most worth logging are the ones most likely to prevent logging — a full disk fails this write along with everything else — so a caller must never be in a position where recording a failure causes one. That is also why the escalation path for a substrate failure is the notification channel rather than this file (execution.md, *Detection is free; recovery is not Steward's*).
"""

import os
from pathlib import Path

from .._util.jsonl import utc_now

LOG_BOUND = 1 << 20
"""Bytes `steward.log` may reach before its oldest lines are dropped.

A megabyte is thousands of lines of a file that a healthy run does not write to at all, so reaching it means something is failing on a cadence — and the newest thousands of lines describe that just as well as the oldest ones would.
"""


def steward_log(path: Path, message: str) -> None:
    """Record one line about the machinery.

    Never raises. A caller writing here is usually already handling a failure, and a logger that can add a second one is worse than no logger.

    Args:
        path: `steward.log`. Created, along with its directory, if absent.
        message: What happened, as one line. Newlines are folded so a traceback cannot become several records.
    """
    line = f"{utc_now()} {' '.join(message.split())}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError:
        # the disk is full, the directory is gone, or the file is not ours --
        # all of which are what this exists to record, and none of which it can
        return


def truncate_log(path: Path, bound: int = LOG_BOUND) -> None:
    """Drop the oldest lines once the file has outgrown its bound.

    **Once per turn rather than on every append**, because `steward_log` is fifteen lines whose one hard property is that they cannot fail, and a read-rewrite on the same path would be several more chances to. A caller writing here is already handling a failure; the bound is bookkeeping and belongs where bookkeeping goes.

    Never raises, for the same reason the writer does not. A file that could not be truncated is a file that is too big, which costs disk and nothing else.

    Args:
        path: `steward.log`. An absent or already-small file is left alone.
        bound: Bytes the file may reach. Roughly half of it survives a truncation, so the next one is thousands of lines away rather than immediately.
    """
    try:
        if not path.exists() or path.stat().st_size <= bound:
            return
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            # seek past the oldest half rather than reading the whole file: the
            # point of a bound is that the file is large, and reading all of it
            # to keep the end of it would be the cost this avoids
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, path.stat().st_size - bound // 2))
            # the first line is almost certainly cut in half by that seek
            stream.readline()
            kept = stream.read()
    except OSError:
        return

    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(kept, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        # `missing_ok` covers the temporary never having been created; it does
        # not cover the unlink itself failing, and a cleanup that raised out of
        # a handler would break the one promise this module makes — in the
        # middle of a turn that has already happened (`_write_status` guards the
        # same way, for the same reason)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
