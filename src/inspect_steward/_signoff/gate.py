"""What stands between a run and its attestation.

**Every blocker at once, coarsest first.** An operator who fixes one refusal and is met by another has walked exactly the loop this gate exists to collapse, so the answer is the whole list — the same discipline `_launch._refusal` already keeps for the archive gate, one level up.

**The refusal is a routing instruction, never a quality bar.** What is refused is not a hole but an *unnamed* one. "8 samples accepted as truncated by an operator" is a signed statement; the same eight passing silently is what this machine exists to prevent. So every message ends by naming the command that answers it, and none of them says *fix this first*.

**Pure over a turn.** Nothing here reads a file or takes a clock: the gate is a function of the turn the verb just ran, which is what lets every refusal be tested against state assembled by hand.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .._tend.items import ORPHAN_RUNNING, UNREADABLE, finding_label, unfinished
from .._workspace import Signature

if TYPE_CHECKING:
    from .._tend import TendResult

EMPTY = "empty"
"""There are no results for a signature to be about — no tasks, or no samples under them."""

UNSETTLED = "unsettled"
ORPHANS = "orphans"
OPEN_WINDOW = "open_window"
UNREAD = "unread"
UNFINALIZED = "unfinalized"
"""Not raised by `check` at all: the terminal fold happens past the gate, and this is `sign._finalize_scan` refusing in the shape the caller already renders."""
UNDECIDED = "undecided"
FAILED = "failed"
STANDING = "standing"
UNSIGNED = "unsigned"
"""Not a gate blocker at all, and the odd one out on purpose.

Every other kind here is a reason the signature was *not* taken. This one is reported after it was: the signature is in the journal, and something un-signed it between recording and returning — the only path being a finding that the terminal scan finalize revealed after the gate had passed (`sign._signoff`). It travels as a blocker because a blocker is the shape the caller already renders and already treats as *this run is not signed*, which is exactly what is true.
"""


@dataclass(frozen=True)
class Blocker:
    """One reason this run cannot be signed, and the act that answers it."""

    kind: str
    summary: str
    """What is in the way, in one sentence."""

    remedy: str
    """The command or the decision that clears it."""

    def __str__(self) -> str:
        return f"{self.summary} — {self.remedy}"


def check(result: "TendResult", signature: Signature | None) -> list[Blocker]:
    """Everything standing between this run and a signature.

    Ordered coarsest first: a run still working has nothing to attest to, so its refusal comes before the one about a single undecided sample.

    Args:
        result: The turn the verb just ran, which supplies every fact checked here.
        signature: The attestation in force, or `None` where nobody has signed.

    Returns:
        Every blocker, in the order an operator should read them. Empty means the run can be signed.
    """
    blockers: list[Blocker] = []
    remaining = unfinished(result.summary, result.acknowledged)
    if empty := _nothing_to_attest_to(result, remaining):
        # **before every other refusal, because it is about whether there is a
        # subject at all.** Each one below asks whether some part of the results
        # is unsettled; this asks whether there are results
        blockers.append(empty)
    if remaining:
        # **naming the verbs that end a run without signing it**, because an
        # abandoned project publishes nothing and that is the correct outcome
        # (workflow.md §13.2) rather than a gap to work around. A hole is
        # accepted by *ruling* on it, which is how it gets a name in the
        # signature
        blockers.append(
            Blocker(
                kind=UNSETTLED,
                summary=(
                    f"{remaining} task{'s' if remaining != 1 else ''} "
                    f"{'have' if remaining != 1 else 'has'} not settled"
                ),
                remedy=(
                    "let the run finish, accept the holes with "
                    "`steward rule --disposition accept`, or end it without "
                    "signing (`steward pause`, `steward timer disarm`)"
                ),
            )
        )
    if orphans := [item for item in result.items if item.kind == ORPHAN_RUNNING]:
        # curation needs a still directory, and this is the one condition that
        # says it is not one -- a refusal none of the design documents named
        blockers.append(
            Blocker(
                kind=ORPHANS,
                summary=(
                    f"{len(orphans)} worker{'s' if len(orphans) != 1 else ''} "
                    f"{'are' if len(orphans) != 1 else 'is'} still running a "
                    f"task the definition no longer names, so which logs are "
                    f"superseded is not yet settled"
                ),
                remedy="wait for them to exit, or stop them",
            )
        )
    if open_windows := list(result.anomalies.open):
        # **including `limit:`, where the readiness item deliberately excludes
        # it.** The item is an invitation to the adjudication conversation and
        # would hide the line that leads an operator to it; the signature is the
        # end of that conversation, and an operator kill nobody ruled on is a
        # caveat missing from the record it is supposed to be complete
        keys = sorted({anomaly.class_key for anomaly in open_windows})
        one = len(keys) == 1
        named = ", ".join(f"{finding_label(key)} (`{key}`)" for key in keys)
        blockers.append(
            Blocker(
                kind=OPEN_WINDOW,
                summary=(
                    f"{len(keys)} finding{'' if one else 's'} "
                    f"{'has' if one else 'have'} no decision yet: {named}"
                ),
                remedy="`steward rule CLASS --disposition ... --reason ... --by NAME`",
            )
        )
    if unread := [item for item in result.items if item.kind == UNREADABLE]:
        # **the hole nobody can size.** Every other refusal here is about a
        # population somebody can count; this one is about a log whose contents
        # are unknown, so what the numbers are over is unknown too. An
        # acknowledgment is the answer and already a caveat in those words --
        # *the numbers are over what could be read* -- which makes this the
        # ordinary shape of the gate rather than an exception to it: a hole
        # with a name on it is signed over, and one without is refused
        one = len(unread) == 1
        blockers.append(
            Blocker(
                kind=UNREAD,
                summary=(
                    f"{len(unread)} log{'' if one else 's'} in the log directory "
                    f"will not read, so what these numbers are over is not known: "
                    + ", ".join(sorted(item.id for item in unread))
                ),
                remedy=(
                    "`steward ack unreadable:NAME --by NAME --reason ...` records "
                    "why the results stand without it, and the signature then "
                    "carries it as a caveat — or repair the file and tend again"
                ),
            )
        )
    if undecided := _undecided(result):
        blockers.append(
            Blocker(
                kind=UNDECIDED,
                summary=(
                    f"{undecided} errored sample{'s' if undecided != 1 else ''} "
                    f"{'are' if undecided != 1 else 'is'} covered by no ruling"
                ),
                remedy=(
                    "rule the classes they belong to; `accept` and `dismiss` "
                    "are answers, and a signed exception is the point"
                ),
            )
        )
    if result.failures:
        # **the turn could not do what it decided, and the gate's own inputs
        # came from that turn.** An acceptance is the case that makes this a
        # refusal rather than a warning: the ruling settles the window and the
        # latch subtracts the task from unfinished work, so a failed log
        # amendment leaves the gate looking at a run that reads settled while
        # a log on disk still says `error` and carries no acceptance
        # provenance. Nothing was journalled for it either, so the next
        # signoff simply applies it again
        count = len(result.failures)
        blockers.append(
            Blocker(
                kind=FAILED,
                summary=(
                    f"this turn could not carry out {count} thing"
                    f"{'s' if count != 1 else ''} it decided: "
                    + "; ".join(result.failures)
                ),
                remedy=(
                    "run it again — an action that failed is retried by the "
                    "next turn, and one that keeps failing is a defect to look at"
                ),
            )
        )
    if signature is not None and result.signed:
        # **`result.signed` rather than a test of its own**, and the sharing is
        # the point: the verdict reads 🔒 from the same predicate, so a run
        # cannot report itself signed while this offers to sign it again
        # (`_tend.items.signed_off`)
        blockers.append(
            Blocker(
                kind=STANDING,
                summary=(
                    f"{signature.by} signed this run at {signature.ts} and "
                    f"nothing has changed since"
                ),
                remedy="nothing needs signing; `--again` records a second signature anyway",
            )
        )
    return blockers


def _nothing_to_attest_to(result: "TendResult", remaining: int) -> Blocker | None:
    """Whether this run has any results for a signature to be about.

    **The gate asked only whether anything was *unfinished*, and nothing is unfinished about nothing.** An empty manifest reports zero missing and zero incomplete, so a capture that enumerated no tasks — a definition edited to nothing, an argument that filtered every task away, a `-T` typo — passed every refusal here and received a signature attesting to an empty set. A task with an empty dataset does the same thing one level down, once its `0/0` log lands `success`: every count is zero and every count agrees.

    The readiness *item* already had this guard (`_tend.items._signoff` returns nothing where `summary.tasks` is zero), which is the tell: the invitation knew there was nothing to invite anybody to, and the verb did not require the invitation.

    **Refused rather than warned, and it is the one refusal with no remedy naming a decision.** Everywhere else the gate routes an operator to the act that names a hole, because a named hole is signable. There is no naming act for this one: a signature over no results is not a caveated attestation, it is a statement about nothing, and the record's whole value is that it cannot be that.

    Args:
        result: The turn being judged.
        remaining: Tasks neither finished nor settled by a decision. The zero-samples half is asked only of a run with none, because a run that has not started yet has no samples either and `UNSETTLED` is already its answer — two refusals for one condition is the loop this gate collapses.
    """
    if result.summary.tasks == 0:
        return Blocker(
            kind=EMPTY,
            summary="this capture enumerated no tasks, so there are no results to accept",
            remedy=(
                "check the definition and its arguments, launch the work, and "
                "sign when there is something to sign for"
            ),
        )
    observed = result.observed
    if (
        not remaining
        and observed is not None
        and not any(task.observed_samples for task in observed.tasks)
    ):
        return Blocker(
            kind=EMPTY,
            summary=(
                f"{result.summary.tasks} task"
                f"{'s' if result.summary.tasks != 1 else ''} finished and not "
                f"one sample was produced, so there are no results to accept"
            ),
            remedy=(
                "check the dataset the tasks are over — a signature says a "
                "operator accepted these results, and there are none"
            ),
        )
    return None


def _undecided(result: "TendResult") -> int:
    """Errored samples in a current log that no ruling in force covers.

    The fold `dispositions` already computes for the errored cell's split, read here for the one question this gate asks of it (`_tend.rulings.Dispositions`).
    """
    return sum(
        counts.get(UNDECIDED, 0) for counts in result.dispositions.by_task.values()
    )


__all__ = ["Blocker", "check"]
