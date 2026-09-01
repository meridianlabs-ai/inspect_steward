"""Leaving `logs/` holding exactly what the attestation covers.

**The argument for curating here is not tidiness, it is that this is the only moment "superseded" is unambiguous** (workflow.md §13.1). Mid-run the answer flickers: a log may be superseded by an attempt still in flight, the newest log by timestamp may be incomplete while an older one finished, and a re-run authorized at 11pm may itself fail and leave its predecessor as the current result after all. Signoff is the point where every task has settled and the gate has pinned a manifest digest, so *the signed set* is exactly definable — and it is definable at no other time.

**Steward never deletes an eval log**, so this is a move into `logs-archive/` and nothing else (`_evalset.archive`). Reversible, journalled with its reason, and visible in a sibling directory rather than merely promised.
"""

from dataclasses import dataclass

from .._evalset.archive import archive_log
from .._evalset.observe import (
    LogAttempt,
    ObservedTasks,
    TaskObservation,
    TaskState,
)


@dataclass(frozen=True)
class Superseded:
    """One log the signature does not cover, and the task it belonged to."""

    location: str
    identifier: str
    key: str
    """The task's display key, for a message a person reads."""


@dataclass(frozen=True)
class Curated:
    """What curation moved, and what would not move."""

    moved: list[tuple[Superseded, str]]
    """Each archived log, with where it now is."""

    failures: list[str]
    """Moves that did not happen, one sentence each."""


def plan(observed: ObservedTasks) -> list[Superseded]:
    """Every attempt a live task has superseded, oldest first.

    **The rule is *not current*, never *the status looks bad*.** A task latched short by an acceptance has a current log whose status is `error`, and that log is the result the attestation covers — with a caveat in `anomalies.md`. Choosing on status would archive the evidence for the exception the signature just recorded.

    **An orphan is the exception, and its every attempt goes — the current one included.** Nothing belonging to an identifier the definition no longer names belongs in the directory a signature covers. A tend ordinarily archives those the moment it meets them (`_schedule.reconcile._archiving`), leaving nothing here to find; but a **paused** run makes no changes to itself, so reconcile archives nothing while the pause stands, and signoff permits a paused run to be signed. Between them that left orphan results sitting in `logs/` under a signature that does not cover them, the timer disarmed, and no turn ever coming to tidy up. So this takes whatever is left rather than assuming the other half ran — and the two cannot fight over a file, because the claim is held across both.

    Pure — no filesystem, no clock.

    Args:
        observed: The manifest read against the log directory, as the gate's turn saw it.

    Returns:
        The logs to move, in the order they were created.
    """
    return [
        Superseded(location=attempt.location, identifier=task.identifier, key=task.key)
        for task in observed.tasks
        for attempt in sorted(_leaving(task), key=lambda attempt: attempt.created)
    ]


def _leaving(task: TaskObservation) -> list[LogAttempt]:
    """The attempts of one task that do not belong in the signed directory."""
    if task.state is not TaskState.ORPHANED:
        return list(task.superseded)
    return [*task.superseded, *([task.current] if task.current is not None else [])]


def curate(superseded: list[Superseded], log_dir: str) -> Curated:
    """Move each superseded attempt into the archive.

    **A move that fails is reported and does not fail the signature.** The signature is the person's act; a filesystem that would not cooperate must not unmake a decision they already made, and a log that could not be archived is still a log — the direction this design fails in everywhere. The next signoff tries again.

    Args:
        superseded: What `plan` selected.
        log_dir: The run's log directory, which the archive is derived from.

    Returns:
        What moved and what did not.
    """
    moved: list[tuple[Superseded, str]] = []
    failures: list[str] = []
    for log in superseded:
        try:
            moved.append((log, archive_log(log.location, log_dir)))
        except OSError as ex:
            failures.append(f"{log.location} could not be archived: {ex}")
    return Curated(moved=moved, failures=failures)
