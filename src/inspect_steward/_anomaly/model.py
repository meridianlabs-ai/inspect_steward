"""Anomalies as structured state: a failure population, and the decision it is owed.

An errored sample is not a bug to fix, it is a question with exactly four honest answers — re-run, exclude, zero, or score as-is — and today the silent default is exclusion, applied by nobody (workflow.md §12, execution.md §6.8). This model makes the question durable: instances group into a **class** (`_evalset.classify`), a class opens a **window** that absorbs instances until somebody rules, and the **ruling** — with its reason, its author, and its report-facing effect — is what the run's story is made of.

**A window closes on a ruling, never on a clock.** Recurrence after a ruling opens a new generation carrying every prior ruling as precedent, attached wherever the anomaly surfaces rather than looked up (workflow.md §12.8) — the 2am agent inherits the 11pm decision without asking for it.

**The class key is opaque here.** Composed and parsed only by detection; this model stores it, groups by it, and prints it. Message text is never in it, so nothing here needs to know what one looks like.

Everything is frozen and pure: state is a fold over the journal (`fold.read_anomalies`), which is what makes crash recovery the ordinary code path and `status` an honest preview.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class AnomalyState(StrEnum):
    """Where one window stands. Open means *not settled* — a decision is still owed or pending."""

    OPEN = "open"
    """Absorbing instances; nobody has picked it up."""

    INVESTIGATING = "investigating"
    """The agent is working it — which stops a fresh session re-proposing, and lets `status` say so."""

    PROPOSED = "proposed"
    """Covered by a live proposal; the question is in front of a person."""

    RULED = "ruled"
    """A `rerun` was authorized and its outcome has not been observed. Closed to new instances, and still open in the sense the verdict cares about: the run is not resolved."""

    RESOLVED = "resolved"
    """Over, with nothing to mark: a re-run passed, or the class was dismissed."""

    ACCEPTED = "accepted"
    """Over, with a mark the report carries: excluded, zeroed, scored, or accepted with a caveat."""


TERMINAL = frozenset({AnomalyState.RESOLVED, AnomalyState.ACCEPTED})
"""The two states nothing further happens to. Everything else is *open* — the definition the verdict and the signoff gate use (workflow.md §12.2: resolved = no anomaly open)."""

ABSORBING = frozenset(
    {AnomalyState.OPEN, AnomalyState.INVESTIGATING, AnomalyState.PROPOSED}
)
"""The states a new instance lands in. RULED is deliberately not here: a ruling closes the window, so what fails after it is either the authorized re-run's outcome (a resolution) or a new generation."""


class Disposition(StrEnum):
    """The answers a ruling can give.

    The doctrinal four for errored samples — `rerun`, `exclude`, `zero`, `score` — plus two anomaly-level closures: `accept` (the data stands, with a caveat the report carries; refused for `error:` classes, where accept-as-is is silent exclusion wearing a decision's clothes) and `dismiss` (looked, nothing here; no mark).
    """

    RERUN = "rerun"
    EXCLUDE = "exclude"
    ZERO = "zero"
    SCORE = "score"
    ACCEPT = "accept"
    DISMISS = "dismiss"


class Outcome(StrEnum):
    """How a pending re-run turned out, as a tend observed it."""

    RERAN_PASSED = "reran_passed"
    RERAN_FAILED = "reran_failed"


SAMPLE_MARKS = frozenset({Disposition.EXCLUDE, Disposition.ZERO, Disposition.SCORE})
"""The dispositions that mark per-sample data — meaningful only where the residue is sample-shaped."""


def honest(kind: str, disposition: Disposition) -> bool:
    """Whether a disposition can honestly be recorded against a class of this kind.

    The matrix `steward rule` and `propose` refuse on, and the one a policy ruling re-checks at application time — one definition, so a pattern in `_steward.yaml` cannot grant what a person could not type. Two rows: `accept` on an `error:` class is silent exclusion wearing a decision's clothes, and the three sample marks mean nothing where the residue is not sample-shaped.

    Args:
        kind: The class key's first segment — `error`, `limit`, `task`, or `score`.
        disposition: The answer being considered.

    Returns:
        Whether recording it would be honest.
    """
    if disposition is Disposition.ACCEPT:
        return kind != "error"
    if disposition in SAMPLE_MARKS:
        return kind in ("error", "limit")
    return True


@dataclass(frozen=True)
class Evidence:
    """What one window absorbed, capped for the journal.

    The full instance membership deliberately is not here: it re-derives from the log directory every turn (`_tend.detect`), and the journal carries enough to read the story — counts, a few ids, the tasks and logs involved, and one verbatim message.
    """

    count: int = 0
    """Instances this window absorbed."""

    samples: tuple[str, ...] = ()
    """A few `id:epoch` pairs, capped — enough to go look, never the census."""

    tasks: tuple[str, ...] = ()
    """Task identifiers the instances belong to."""

    logs: tuple[str, ...] = ()
    """Log locations, which is what makes the window investigable (workflow.md §12.5)."""

    exemplar: str = ""
    """One error message, verbatim and truncated. Display only — never identity."""

    first_ts: str = ""
    """When the first instance was absorbed."""

    last_ts: str = ""
    """When the most recent one was."""


@dataclass(frozen=True)
class Ruling:
    """One decision about one class, as the journal records it."""

    class_key: str
    disposition: Disposition

    reason: str
    """Why — required, because this is the only account of the decision that survives."""

    by: str
    """Who decided — free text naming a person, or `policy` when a standing pre-authorization applied (step 25). Never a role: a ruling is never the agent's own (agent.md §6)."""

    ts: str
    """When. Load-bearing beyond display: it closes the window, and step 25 forgives stall history before it."""

    proposal: str | None = None
    """The covering proposal, when the ruling answered one — what lets a group decision be unpicked."""

    effect: str = ""
    """The report-facing sentence for dispositions that mark the data (`2 of 500 samples excluded from scoring`). Empty for `rerun` and `dismiss`, which mark nothing."""


@dataclass(frozen=True)
class Resolution:
    """What a tend observed happen after a `rerun` ruling."""

    outcome: Outcome
    detail: str
    ts: str


@dataclass(frozen=True)
class ProposalEvidence:
    """One class's weight, as the proposal snapshotted it from the fold.

    Snapshotted by the verb rather than referenced, so the record shows what the human was shown — and so they can answer *some* of a proposal: per-class evidence is what makes partial acceptance an informed act rather than a blind one.
    """

    count: int = 0
    exemplar: str = ""
    first_ts: str = ""
    last_ts: str = ""

    precedent: tuple[str, ...] = ()
    """Prior rulings on this class, as short lines."""


@dataclass(frozen=True)
class Proposal:
    """The agent's grouping judgement: these classes are one decision.

    One `action` per proposal, deliberately — classes wanting different dispositions are two proposals. Optional ceremony, never a gate: with no agent, classes stand alone and `steward rule CLASS` works bare (workflow.md §12.4).
    """

    id: str
    """`prop-<digest8>` — short enough to type, unique enough to record."""

    action: Disposition
    classes: tuple[str, ...]
    evidence: dict[str, ProposalEvidence] = field(
        default_factory=dict[str, ProposalEvidence]
    )
    reason: str = ""
    by: str = ""
    ts: str = ""


@dataclass(frozen=True)
class Anomaly:
    """One window of one class: the population, its state, and the decisions around it."""

    class_key: str
    kind: str
    """The class key's first segment — `error`, `limit`, `task`, or `score`."""

    state: AnomalyState
    evidence: Evidence

    substrate: bool = False
    """Whether the failure is the machinery under the run. A substrate class gets no re-run proposal until a person has looked (execution.md §9.1)."""

    generation: int = 1
    """Which window this is, 1-based. A ruling closes a window; recurrence opens the next generation."""

    opened_ts: str = ""

    note: str = ""
    """The most recent investigation note, for `status` to say what is being worked."""

    proposal: str | None = None
    """The live proposal covering this window, if one does."""

    ruling: Ruling | None = None
    """The ruling in force. Last wins; superseded ones fold into `precedent`."""

    resolution: Resolution | None = None
    """The most recent observed outcome of a pending re-run."""

    precedent: tuple[Ruling, ...] = ()
    """Every earlier ruling on this class, oldest first — prior windows and superseded decisions. Attached, never a lookup (workflow.md §12.8)."""

    failed_resolutions: int = 0
    """Re-runs this window that failed again — what re-arms the recurrence-review item."""

    refs: frozenset[str] = frozenset()
    """The content-derived ref of every instance this window absorbed — the window's full membership, rebuilt from its `instance` events at fold time.

    What a ruling on this window covers, exactly. Attempt instants cannot say that: a failure appearing *after* the ruling inside the same still-running attempt predates nothing, opens the next generation, and must not be re-run under a decision that never saw it — so the executor's applicable set, the pass check's remainder, and the dispositions report all key on these refs instead. Distinct from `evidence.samples`, which is capped display material."""

    failed_refs: dict[str, str] = field(default_factory=dict[str, str])
    """Ref to resolution instant, for every re-run failure recorded against this window.

    A re-run's failure replaces the record the ruling covered (a requeue supersedes the old row under a fresh uuid; a landed retry writes a new attempt), so its ref lives here rather than in `refs` — and it carries its instant because coverage is per ruling: the ruling that authorized the re-run must never re-apply to its own outcome, while a **later** ruling on this window covers exactly these refs (`fold.covered_refs`). Without this record, re-ruling after `reran_failed` would find nothing applicable and the window could pass without the failed sample ever re-running."""

    @property
    def effect(self) -> str:
        """The mark the report carries, from the ruling in force."""
        return self.ruling.effect if self.ruling is not None else ""

    @property
    def open(self) -> bool:
        return self.state not in TERMINAL


@dataclass(frozen=True)
class Anomalies:
    """Everything the journal says about this run's anomalies, folded once per turn."""

    open: tuple[Anomaly, ...] = ()
    """Windows not yet settled — what gates the signoff and drives the verdict."""

    settled: tuple[Anomaly, ...] = ()
    """Terminal windows, for history and (step 26) `anomalies.md`."""

    proposals: dict[str, Proposal] = field(default_factory=dict[str, Proposal])
    """Live proposals — those with at least one covered class still awaiting a ruling."""

    absorbed_refs: dict[str, frozenset[str]] = field(
        default_factory=dict[str, frozenset[str]]
    )
    """The dedupe ledger: per class, the content-derived ref of every instance the journal has absorbed — across every window, so a ruling that closes one cannot make the same log's errors read as news. Refs rather than counts, deliberately: a count cannot see one sample's failure replaced by another's in the same eval (a requeue's ordinary shape), and a ref diff is exact. What the absorb step diffs detection's projection against."""

    def accepted(self) -> tuple[Anomaly, ...]:
        """Settled windows that carry a mark — the `anomalies.md` filter, ready for step 26."""
        return tuple(
            anomaly
            for anomaly in self.settled
            if anomaly.state is AnomalyState.ACCEPTED
        )

    def absorbing(self, class_key: str) -> Anomaly | None:
        """The window currently absorbing instances of a class, if one is."""
        return next(
            (
                anomaly
                for anomaly in self.open
                if anomaly.class_key == class_key and anomaly.state in ABSORBING
            ),
            None,
        )

    def of_class(self, class_key: str) -> tuple[Anomaly, ...]:
        """Every window of a class, oldest generation first."""
        return tuple(
            sorted(
                (
                    anomaly
                    for anomaly in (*self.open, *self.settled)
                    if anomaly.class_key == class_key
                ),
                key=lambda anomaly: anomaly.generation,
            )
        )


def composed_effect(anomalies: Anomalies, key: str, decided: Disposition) -> str:
    """The report-facing sentence a marking disposition composes when nobody wrote one.

    One composition shared by `steward rule` and a policy ruling, so the sentence the record carries cannot depend on which path recorded it. Empty for the dispositions that mark nothing — `rerun` and `dismiss` — and for `accept`, whose effect only a person can write.

    Args:
        anomalies: The fold, for the class's open population.
        key: The class being ruled.
        decided: The disposition being recorded.

    Returns:
        The sentence, or empty where the disposition composes none.
    """
    if decided not in SAMPLE_MARKS:
        return ""
    count = sum(
        anomaly.evidence.count for anomaly in anomalies.open if anomaly.class_key == key
    )
    plural = "" if count == 1 else "s"
    if decided is Disposition.EXCLUDE:
        return f"{count} sample{plural} excluded from scoring"
    if decided is Disposition.ZERO:
        return f"{count} sample{plural} scored zero"
    return f"{count} sample{plural} scored as recorded"


__all__ = [
    "ABSORBING",
    "SAMPLE_MARKS",
    "TERMINAL",
    "Anomalies",
    "Anomaly",
    "AnomalyState",
    "Disposition",
    "Evidence",
    "Outcome",
    "Proposal",
    "ProposalEvidence",
    "Resolution",
    "Ruling",
    "composed_effect",
    "honest",
]
