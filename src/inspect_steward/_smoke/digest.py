"""The artifact a smoke leaves behind, and the one thing an agent is told to trust.

**Named in the runbook before it existed.** *Trust the artifact, not the exit code* (agent.md §9) lists four of them — the manifest delta, the smoke digest, the log itself, the anomaly count — and the second had no definition anywhere in the design. This is it. The rule matters most here of all four, because a rehearsal under `fail_on_error=False` exits zero for most of the things it is looking for: a scorer that threw, a sandbox that never started, a model on a fallback context window. A clean exit means a process ended.

**Ordered by what a reader has to decide.** The verdict first, then what ran and what those samples did, then the checks, then the findings, then the windows. A reader who takes only the first line should have taken the true one.

**It says nothing about tokens**, which is a deliberate subtraction rather than an omission. A rehearsal is a couple of samples off the front of each unshuffled dataset; multiplying what they spent by the run's population produces a confident-looking number from a sample that was never a draw, and the design already refuses the same arithmetic for durations on the same grounds.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .._evalset.classify import kind_of
from .._evalset.instances import Instance
from .._tend.items import class_summary
from .checks import Probe, Verdict, Window


class Outcome(StrEnum):
    """How the rehearsal ended."""

    PASSED = "passed"
    """It ran to completion and every check that could answer, answered well."""

    FAILED = "failed"
    """A check failed, a task errored, or the run did not produce what it needed to. The launch this precedes is not ready."""

    CAPPED = "capped"
    """The wall-clock deadline fired and the rehearsal had nothing to show for it — no sample landed, so no check could answer. A failure, and a distinct one: a check that came back wrong is a configuration to fix, and this is a rehearsal that never got far enough to have an opinion.

    **Not every rehearsal the cap interrupted.** Truncation is the expected way for a smoke to end — it runs a couple of samples off the front of each task precisely so it can be stopped — so a cap that fires with samples landed and every check answered is `PASSED`, and goes unremarked. This outcome is the narrow case where the deadline arrived before anything was established at all."""


@dataclass(frozen=True)
class Satisfied:
    """A task the rehearsal left out because the log store already answers it."""

    identifier: str
    key: str
    """The task as the operator knows it."""
    source: str
    """Where the store had it — named, because an identifier match says nothing about the environment the log ran in, and whose result this is has to stay answerable."""


@dataclass(frozen=True)
class Smoke:
    """What one rehearsal established.

    `--json` renders this whole value through `dataclasses.asdict`, exactly as `steward status --json` does, so nothing here needs a serializer of its own.
    """

    outcome: Outcome = Outcome.PASSED
    identifiers: tuple[str, ...] = ()
    """Task identifiers rehearsed — the half of what a later `launch` matches its own capture against that can be reported per task."""

    digest: str = ""
    """The manifest digest rehearsed — the other half, and the one that notices a dataset that grew or an `epochs` that doubled, neither of which moves a task identifier. Comparable at all only because the slice rides the workers and never the capture."""

    scanners: str = ""
    """`scan_digest` of the material the rehearsal scanned under, which `digest` does not cover: `Manifest.scan` is not hashed into it.

    A digest rather than the names. Names would report a launch as rehearsed after somebody changed a scanner's parameters, its scan-side model, or the filter deciding which transcripts it sees — every one of which changes what the rows say while leaving the names identical.
    """

    scan_model: str = ""
    """The model those scanners reviewed with, or empty where none was configured and each sample's own model stood in. Recorded for the same reason and checked the same way — the rehearsal established a window for this model and nothing about another."""

    probe: Probe = field(default_factory=Probe)
    waived: tuple[str, ...] = ()
    """Checks accepted rather than passed. Recorded so a pass is honest about what it did not establish."""

    tasks: int = 0
    landed: int = 0
    """Samples the rehearsal actually produced."""

    population: int = 0
    """Samples the real run would produce, from the untruncated capture. A count rather than an estimate, and the thing a reader is actually sizing up — the rehearsal ran `landed` of them."""

    findings: tuple[str, ...] = ()

    failures: tuple[str, ...] = ()
    """Sample-level failures, one sentence per class, in the tend's own words — errored samples and operator-terminated ones alike.

    **The half of a rehearsal that a green exit hides.** Workers force `continue_on_fail`, so a bad key, a sandbox image that will not start, or a scorer that throws all land as errored samples inside a log whose status is `success` and whose task finished. Those are the failures a smoke exists to find, and reading only the task's status finds none of them.
    """

    errored: int = 0
    """Samples that errored. What makes a rehearsal fail, where an operator limit in `failures` is reported and does not: a sample stopped by a message or token limit ran as designed."""

    threw: int = 0
    """Transcripts a scanner threw on.

    Also blocking, and it was not. These arrive as `scanerror:` classes among `findings`, which count toward nothing — so every scanner could fail on every transcript while the verdict read *rehearsed and ready*. During a run the same class is a question for an operator, since the samples are fine and only the reading of them failed; before one it is the scan path saying it does not work.
    """

    errors: tuple[str, ...] = ()
    """What went wrong running the rehearsal itself — a task that failed, a scan that would not fold. Distinct from a check coming back negative, and from a sample that errored."""

    elapsed: float = 0.0
    samples: int = 0
    cap: int = 0
    log_dir: str = ""

    capped: bool = False
    """Whether the deadline fired before the tasks settled.

    **Carried, and deliberately never reported.** A smoke runs a couple of samples under a clock so that it can be stopped; a sample the deadline cut short is the tool working, not a fact about the definition, and there is nothing for a reader to do about it. So it reaches no verdict line, no digest section and no journal field — it exists because `_amended` re-derives the outcome after a failed write and has to know what the deadline already excused (`_smoke.run.unfinished`)."""

    satisfied: tuple[Satisfied, ...] = ()
    """Tasks the log store already answers, which the rehearsal did not run: the launch copies their logs in rather than starting them. Rung 2 of the convergence ladder, consulted read-only with the launch's own predicate (`_store.match`), so the two cannot disagree."""

    store: str | None = None
    """The store consulted, or `None` where none was configured."""

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASSED

    @property
    def blocked(self) -> tuple[str, ...]:
        """Checks that failed and were not waived, which is what makes the outcome."""
        return tuple(
            check.name
            for check in self.probe.checks
            if check.blocks and check.name not in self.waived
        )

    @property
    def waived_away(self) -> tuple[str, ...]:
        """Checks that would have stopped the launch and were accepted instead.

        **Not everything `--accept` named**, which is what makes the count honest. Waiving a check that then passed established nothing and hid nothing, and a verdict reading *ready (1 waived)* over it would claim a caveat the rehearsal does not have.
        """
        return tuple(
            check.name
            for check in self.probe.checks
            if check.blocks and check.name in self.waived
        )


def outcome(
    probe: Probe,
    *,
    waived: Iterable[str],
    capped: bool,
    errors: int,
    errored: int = 0,
    threw: int = 0,
    landed: int = 0,
) -> Outcome:
    """Decide a rehearsal's verdict from what it found.

    **A cap is not a kind of failure, and treating it as one made a good rehearsal unlaunchable.** A smoke runs a couple of samples off the front of each task under a deadline; ending inside a sample is the expected way for it to stop, not a defect in the definition. The cap used to short-circuit this function ahead of everything else, so a rehearsal that answered every check and landed all but one of its samples was reported as having established nothing, and the launch it had just cleared was refused. What the deadline actually costs is coverage, and `landed` already says what was covered.

    **So the cap decides nothing on its own, and only speaks where the rehearsal is otherwise clean.** A deadline that fires before a single sample lands *is* worth stopping for, because then no check could answer and there is nothing to have an opinion about — that is `CAPPED`, and it is the only shape of it left.

    **An errored sample fails the rehearsal, and is not waivable.** `--accept` waives a *check* — a question about configuration that an operator can answer better than Steward can, like a pre-deployment model with no registry entry. A sample that errored is not a question: it is the thing a smoke was run to find out, arriving. Somebody who wants to launch anyway already can, because the gate on the real launch only ever warns.

    **A scanner that threw fails it too**, and that one was a hole: those arrive as `scanerror:` classes among the findings, which are reported and counted toward nothing, so every scanner could fail on every transcript under a verdict of *rehearsed and ready*.

    Args:
        probe: What the transcripts were asked.
        waived: Check names accepted rather than passed.
        capped: Whether the deadline fired before the tasks settled.
        errors: How many things went wrong running the rehearsal itself. What the cap itself cut short is not among them (`_smoke.run.unfinished`).
        errored: How many samples errored.
        threw: How many transcripts a scanner threw on.
        landed: How many samples the rehearsal produced. Only consulted under a cap, to tell a rehearsal that was cut short from one that never started.
    """
    accepted = set(waived)
    failed = [one for one in probe.failed if one not in accepted]
    if failed or errors or errored or threw:
        return Outcome.FAILED
    if capped and not landed:
        return Outcome.CAPPED
    return Outcome.PASSED


def findings(instances: Sequence[Instance]) -> tuple[str, ...]:
    """What the scanners said, one sentence per class, in the wording the tend uses.

    **Through `class_summary` rather than a phrasing of its own**, which is the whole reason that function was split out of `anomaly_summary`: a finding described one way before the launch and another way during it is two findings as far as a reader is concerned. What a smoke cannot say is anything a *window* knows — generation, precedent, whether anybody ruled — because none of that exists yet. This is a census of a scan that folded thirty seconds ago.
    """
    counts: dict[str, int] = {}
    for instance in instances:
        counts[instance.class_key] = counts.get(instance.class_key, 0) + 1
    return tuple(
        class_summary(kind_of(key), key, count) for key, count in sorted(counts.items())
    )


def digest_markdown(smoke: Smoke) -> str:
    """Render the digest an operator reads and an agent relays."""
    lines = [
        "# smoke",
        "",
        f"**{_verdict(smoke)}**",
        "",
        f"`{smoke.log_dir}`",
        "",
    ]
    lines.extend(_ran(smoke))
    lines.extend(_satisfied(smoke))
    lines.extend(_failures(smoke))
    lines.extend(_checks(smoke))
    lines.extend(_findings(smoke))
    lines.extend(_windows(smoke.probe.windows))
    lines.extend(_errors(smoke))
    return "\n".join(lines)


def _verdict(smoke: Smoke) -> str:
    """The one line a reader who reads nothing else should have read.

    **Every reason at once, rather than the first one found**, on the rule `signoff` already keeps: a rehearsal can fail its context-window check *and* error half its samples, and an operator who fixes one and is refused for the other has been made to look twice at a document that knew both.
    """
    if smoke.outcome is Outcome.CAPPED:
        return (
            f"🛑 capped at {smoke.cap}m with nothing established — no sample "
            f"landed, so no check could answer"
        )
    if smoke.outcome is Outcome.FAILED:
        reasons: list[str] = []
        if blocked := ", ".join(smoke.blocked):
            reasons.append(f"{blocked} failed")
        if smoke.errored:
            reasons.append(
                f"{smoke.errored} of {smoke.landed} samples errored"
                if smoke.landed
                else f"{smoke.errored} samples errored"
            )
        if smoke.threw:
            reasons.append(
                f"a scanner threw on {smoke.threw} "
                f"transcript{'' if smoke.threw == 1 else 's'}"
            )
        if smoke.errors:
            reasons.append("the rehearsal did not complete")
        what = "; ".join(reasons) or "the rehearsal did not complete"
        return f"🛑 not ready to launch — {what}"
    if not smoke.tasks and smoke.satisfied:
        return (
            f"✅ nothing to rehearse — every task is satisfied from the log "
            f"store at {smoke.store}"
        )
    away = smoke.waived_away
    waived = f" ({', '.join(away)} waived)" if away else ""
    return f"✅ rehearsed and ready{waived}"


def _ran(smoke: Smoke) -> list[str]:
    """What the rehearsal was, and how big the thing it is a rehearsal of is.

    **Counts only, and no arithmetic on top of them.** A digest that said what the run would spend was extrapolating from a handful of samples taken off the front of each unshuffled dataset — not a random draw, and on a small enough sample that the answer carried a false precision nobody should have been deciding on. The population is a count the capture already made, so it stays; the rate that would have turned it into money does not (workflow.md §7.1).
    """
    rehearsed = " rehearsed" if smoke.satisfied else ""
    lines = [
        "## what ran",
        "",
        f"{smoke.tasks} task{'' if smoke.tasks == 1 else 's'}{rehearsed} · "
        f"{smoke.landed} samples in {smoke.elapsed:.0f}s",
        "",
    ]
    if smoke.population:
        lines.extend([f"The run itself is {smoke.population:,} samples.", ""])
    return lines


def _satisfied(smoke: Smoke) -> list[str]:
    """What the rehearsal left out, and where the launch will get it from.

    Named one by one rather than counted, for the reason the launch names its reused rows: a reader deciding whether to accept somebody else's result needs to know it is somebody else's.
    """
    if not smoke.satisfied:
        return []
    lines = ["## satisfied from the log store", ""]
    lines.extend(f"- {one.key} — {one.source}" for one in smoke.satisfied)
    lines.extend(
        [
            "",
            "Not rehearsed: the launch will copy these logs in rather than run them.",
            "",
        ]
    )
    return lines


def _failures(smoke: Smoke) -> list[str]:
    """The samples that did not come back clean, above the checks that come back green.

    Placed immediately after what ran because it is a fact about *this* rehearsal rather than about the configuration behind it, and because a count of samples that landed means something different once you know half of them errored.
    """
    if not smoke.failures:
        return []
    return [
        "## what the samples did",
        "",
        *(f"- {line}" for line in smoke.failures),
        "",
    ]


_MARK = {
    Verdict.PASSED: "✓",
    Verdict.FAILED: "✗",
    Verdict.UNDETERMINED: "?",
    Verdict.UNEXERCISED: "–",
}
"""One character per verdict. `–` and `?` are deliberately not `✗`: *there was nothing to check* and *this could not be checked* are neither failures nor passes, and a reader sorting on the first character must not be told otherwise."""


def _checks(smoke: Smoke) -> list[str]:
    if not smoke.probe.checks:
        return []
    lines = ["## checks", ""]
    for check in smoke.probe.checks:
        waived = " · _waived_" if check.name in smoke.waived_away else ""
        lines.append(
            f"- {_MARK[check.verdict]} `{check.name}` — {check.detail}{waived}"
        )
    return lines + [""]


def _findings(smoke: Smoke) -> list[str]:
    """What the scanners found, or an explicit line saying they found nothing.

    **Silent is not an option here.** A scan that ran and flagged nothing and a scan that never ran produce the same empty list, and this document exists to tell a reader which of those happened before they commit a night to the run.
    """
    lines = ["## what the scanners found", ""]
    if not smoke.tasks and smoke.satisfied:
        return lines + ["Nothing scanned — nothing was rehearsed.", ""]
    if not smoke.findings:
        return lines + ["Nothing flagged.", ""]
    return lines + [f"- {line}" for line in smoke.findings] + [""]


def _windows(windows: Sequence[Window]) -> list[str]:
    """Every model's effective context window, and where each one came from."""
    if not windows:
        return []
    lines = [
        "## context windows",
        "",
        "| model | tokens | from |",
        "| --- | ---: | --- |",
    ]
    for entry in windows:
        if entry.undetermined:
            tokens, source = "?", f"could not check here — {entry.undetermined}"
        elif entry.tokens is None:
            tokens, source = "—", "**nothing — the run assumes 128000**"
        else:
            tokens = f"{entry.tokens:,}"
            source = (
                f"aliased to `{entry.aliased}`" if entry.aliased else "its own entry"
            )
        lines.append(f"| `{entry.model}` | {tokens} | {source} |")
    return lines + [""]


def _errors(smoke: Smoke) -> list[str]:
    if not smoke.errors:
        return []
    return ["## what went wrong", "", *(f"- {line}" for line in smoke.errors), ""]


def echo_smoke(smoke: Smoke) -> list[str]:
    """The terminal's shorter account of the same thing.

    Shorter and never different: the verdict, the checks that decided it, and where to read the rest. A reader at the terminal has the file a keystroke away, and repeating the whole digest into a scrollback is how the file stops being read.
    """
    lines = [_verdict(smoke)]
    if smoke.satisfied:
        count = len(smoke.satisfied)
        lines.append(
            f"  ↩ {count} task{'' if count == 1 else 's'} satisfied from the "
            f"log store — not rehearsed"
        )
        lines.extend(f"    {one.key} — {one.source}" for one in smoke.satisfied)
    for check in smoke.probe.checks:
        mark = _MARK[check.verdict]
        waived = " (waived)" if check.name in smoke.waived_away else ""
        lines.append(f"  {mark} {check.name}: {check.detail}{waived}")
    lines.extend(f"  ✗ {line}" for line in smoke.failures)
    if smoke.findings:
        lines.extend(f"  · {line}" for line in smoke.findings)
    lines.extend(f"  ! {line}" for line in smoke.errors)
    return lines


def journal_fields(smoke: Smoke) -> dict[str, Any]:
    """What the `SMOKED` event carries — enough for a later launch to judge staleness, and no transcript."""
    return {
        "identifiers": list(smoke.identifiers),
        "digest": smoke.digest,
        "scanners": smoke.scanners,
        "scan_model": smoke.scan_model,
        "verdict": str(smoke.outcome),
        "waived": list(smoke.waived),
        "samples": smoke.samples,
        "cap": smoke.cap,
        "log_dir": smoke.log_dir,
        "satisfied": [one.identifier for one in smoke.satisfied],
        "log_store": smoke.store,
    }


__all__ = [
    "Outcome",
    "Satisfied",
    "Smoke",
    "digest_markdown",
    "echo_smoke",
    "findings",
    "journal_fields",
    "outcome",
]
