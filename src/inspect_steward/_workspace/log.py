"""`steward.log` — whether the machinery worked, as opposed to what it found.

The journal answers *was this eval set conducted properly*. A tend that crashed, a spawn that failed, a claim found wedged and killed, a `_steward.yaml` that would not parse — none of those is a fact about the eval, and putting them in the journal would bloat the one record a workspace cannot rebuild, on a ten-minute cadence, with material nobody reviewing the run's conduct wants to read. So the rule is a single question: **is this a fact about the eval set, or about Steward?** (workflow.md, *`steward.log` — whether Steward itself worked*.)

The split answers something neither record could answer alone. A successful tend writes an `observation` to the journal, so *no tend for four hours* is computable from the journal's silence — and *why* is here, where the failures went. Sharing one file would make a run whose tends were all crashing look like a run with nothing to report.

**This is the minimal version.** Bounding, rotation, and the sync outward belong with the step that owns durability of the workspace; what is here is the append and the one property that cannot be added later, which is that it never raises. The conditions most worth logging are the ones most likely to prevent logging — a full disk fails this write along with everything else — so a caller must never be in a position where recording a failure causes one. That is also why the escalation path for a substrate failure is the notification channel rather than this file (execution.md, *Detection is free; recovery is not Steward's*).
"""

from pathlib import Path

from .._util.jsonl import utc_now


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
