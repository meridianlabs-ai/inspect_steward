"""What the rehearsal's transcripts say about how the run is configured.

**The one place Steward reads a whole eval log, and it is a named exception rather than an erosion.** `_evalset/instances.py` states the discipline the rest of the package keeps — headers, sample summaries, and errored samples with `messages` and `events` excluded, never a transcript — and the runbook repeats it to agents. Every question below needs `ModelEvent`s, which live in exactly the place that rule forbids. The exception holds because four bounds hold at once, and it would not hold if any one of them failed:

- **only `.steward/smoke/`, never `logs/`** — enforced by the path this is handed rather than by anybody remembering;
- **bounded by construction** — the smoke's own `--samples` per task, which is two by default;
- **paid once at launch**, not sixty times a night, which is what the context budget in agent.md §8 is actually about;
- **the transcript never leaves Steward.** What travels is a verdict and a count. That is the same shape the anomaly census already has: the evidence stays where it was read and the answer is what moves.

**Four questions, and none of them is answerable any other way.** A run whose model resolved to no context window silently assumes 128000 and silently stops shrinking oversized tool output; a run whose reasoning is not replayed is an agent that forgets what it was thinking between turns; a run whose scanners recorded nothing looks identical to one whose scanners found nothing. All of them produce an eval that completes, scores, and is wrong — the class of failure a rehearsal exists for, and the class a green exit code cannot distinguish from success.

The fourth is the one that does not come from a transcript. `scan_coverage` is handed its two numbers by the fold rather than reading them here, and lives among the others because what it answers is the same kind of question: a property of *this* configuration that a person may be able to vouch for and Steward cannot, which is what makes it waivable by name.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from inspect_ai.event import ModelEvent
from inspect_ai.log import EvalLog, EvalSample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ContentReasoning,
    ContentText,
    get_model,
)
from inspect_ai.model._model_info import get_model_input_tokens

CONTEXT_WINDOW = "context_window"
"""Check name: every model resolved to a context window rather than the default."""

REASONING = "reasoning"
"""Check name: reasoning survived into the next turn's input."""

REASONING_API = "reasoning_api"
"""Check name: reasoning reached the provider's own request body."""

SCAN_COVERAGE = "scan_coverage"
"""Check name: every transcript the rehearsal landed was reviewed by every scanner."""

CHECKS = (CONTEXT_WINDOW, REASONING, REASONING_API, SCAN_COVERAGE)
"""Every check a smoke runs, in digest order. `--accept` takes one of these names."""

_FALLBACK_WARNING = "Unable to determine context window"
"""The one trace the 128000 fallback leaves in a log, from `_compaction._resolve_threshold`.

Corroboration and never the primary signal: it is emitted only when compaction runs with a *fractional* threshold, so its absence proves nothing. The quieter path (`_compaction/summary.py`, where an unresolved window disables oversized-tool-output shrinking) logs nothing at all.
"""


class Verdict(StrEnum):
    """What one check concluded."""

    PASSED = "passed"
    FAILED = "failed"

    UNDETERMINED = "undetermined"
    """The check could not run. Reported, never blocking — a missing provider SDK is a fact about the machine Steward is on, not about the run."""

    UNEXERCISED = "unexercised"
    """There was nothing to check. A non-reasoning model has no reasoning to replay, and calling that a failure would block every such eval on the day this shipped."""


@dataclass(frozen=True)
class Check:
    """One check's verdict, and the sentence a reader needs to act on it."""

    name: str
    verdict: Verdict
    detail: str

    @property
    def blocks(self) -> bool:
        """Whether this check stops the launch. Only an outright failure does."""
        return self.verdict is Verdict.FAILED


@dataclass(frozen=True)
class Window:
    """One model's effective context window, and where it came from."""

    model: str
    tokens: int | None = None
    aliased: str | None = None
    """The database entry the provider aliased onto, where it did. A pre-deployment model with no entry of its own inheriting the current frontier's window is the ordinary case and is fine — but *which* window it inherited is the sentence worth reading, so it is carried rather than collapsed into a boolean."""

    undetermined: str | None = None
    """Why the window could not be established, where it could not. A provider SDK absent from *Steward's* interpreter says nothing about the worker's."""

    @property
    def resolved(self) -> bool:
        return self.tokens is not None


@dataclass(frozen=True)
class Probe:
    """Everything the rehearsal's own logs were asked."""

    checks: tuple[Check, ...] = ()
    windows: tuple[Window, ...] = ()

    @property
    def failed(self) -> tuple[str, ...]:
        """Names of the checks that block, in digest order."""
        return tuple(check.name for check in self.checks if check.blocks)


def probe(logs: Sequence[EvalLog], *, models: Iterable[str]) -> Probe:
    """Ask the rehearsal's transcripts whether the run is configured as intended.

    Args:
        logs: The smoke's own logs, read whole. Never the run's.
        models: Every distinct model the manifest names, plus the scan model where one is configured — a scanner reviewing with a mis-resolved window is the same failure one layer over.

    Returns:
        The checks, and the per-model windows behind the first of them.
    """
    samples = [sample for log in logs for sample in (log.samples or [])]
    windows = tuple(window(name) for name in sorted(set(models)))
    return Probe(
        checks=(
            _context_window(windows, samples),
            *_reasoning(samples),
        ),
        windows=windows,
    )


def scan_coverage(*, reviewed: int, landed: int, scanning: bool) -> Check:
    """Whether the scanners answered for every transcript the rehearsal landed.

    **A scan that recorded nothing looks exactly like a scan that found nothing.** Both produce an empty findings list, no errors and no throws — so a rehearsal whose scanners never wrote a row reported *rehearsed and ready*, having established nothing at all about the path that will review five thousand transcripts tonight. That the scanners **work** is among the three things workflow.md §7.1 says a smoke is for, and a census of what they flagged cannot answer it. Reproduced: a full two-sample log with no scan rows passed.

    **A row per transcript whatever the verdict, which is what makes the count mean anything.** Scout appends a result for every transcript-scanner pair on success and on error alike (`_scan._scan_one`: *always append a result*), recording even the scanner that returned nothing — so a spotless review of four transcripts reads as four, and a shortfall really is transcripts nobody answered for rather than transcripts nobody flagged. The two are worth keeping apart because only one of them is a defect, and a check written over findings would have reported the clean run as the broken one.

    **Nothing at all blocks; anything short of everything is only reported.** A definition's `ScannerConfig.filter` is a SQL clause applied per sample, and a transcript it excludes *is not scanned at all* — no parquet row, no snapshot entry (`inspect_ai._eval.task.scan.scan_eval_sample`). It reaches Steward only as a hash inside the scan spec's metadata, so a partial count cannot be told apart from a correct one, and failing on it would fail every filtered definition on every rehearsal — the gate that fires each time being the gate nobody reads. Zero can be told apart, near enough: a filter narrows *which* transcripts a scanner sees without stopping it running on the ones that match, so a rehearsal where no scanner answered for any transcript is the broken path this check exists for. What it costs is a filter selective enough to match none of a two-sample slice, which is what `--accept` is for.

    Args:
        reviewed: Transcripts every scanner answered for.
        landed: Samples the rehearsal produced, over the tasks the count could be taken for.
        scanning: Whether this run scans at all.

    Returns:
        The check, `unexercised` where there was nothing to review or nothing reviewing, and `failed` only where nothing was reviewed at all.
    """
    if not scanning:
        return Check(SCAN_COVERAGE, Verdict.UNEXERCISED, "this run scans nothing")
    if not landed:
        return Check(
            SCAN_COVERAGE, Verdict.UNEXERCISED, "no samples landed to be reviewed"
        )
    if not reviewed:
        return Check(
            SCAN_COVERAGE,
            Verdict.FAILED,
            f"nothing was recorded for any of the {landed} transcripts",
        )
    if reviewed >= landed:
        return Check(
            SCAN_COVERAGE,
            Verdict.PASSED,
            f"every scanner answered for all {landed} transcripts",
        )
    return Check(
        SCAN_COVERAGE,
        Verdict.PASSED,
        f"{reviewed} of {landed} transcripts were reviewed",
    )


def window(name: str) -> Window:
    """One model's effective context window, by the route the runtime itself takes.

    **`get_model_input_tokens`, not `get_model_info`**, and the difference is the whole check. Providers alias an unknown frontier name onto a known entry *before* any database lookup, so a pre-deployment codename with no entry of its own still gets a real window — and a check written on whether the name resolves would report that correct configuration as a failure. What actually matters is whether the runtime ends up with a number, because the alternative is `DEFAULT_CONTEXT_WINDOW`: 128000 assumed with a log line, and oversized-tool-output shrinking disabled with none.

    Instantiating the model is unavoidable — the aliasing is a provider method — so this is also the one call that can fail for a reason having nothing to do with the run. A missing SDK raises rather than returning `None`, which is what keeps *could not check* separable from *checked, and it is wrong*.
    """
    try:
        model = get_model(name)
    except Exception as ex:
        # deliberately broad. `get_model` raises `PrerequisiteError` for a
        # missing provider package, but the failure being reported here is
        # *this interpreter cannot answer*, and narrowing it would turn an
        # unanticipated provider error into a traceback out of a rehearsal
        return Window(model=name, undetermined=_reason(ex))
    tokens = get_model_input_tokens(model)
    aliased = model.input_tokens_name()
    return Window(
        model=name,
        tokens=tokens,
        aliased=aliased if aliased != model.canonical_name() else None,
    )


def _context_window(windows: Sequence[Window], samples: Sequence[EvalSample]) -> Check:
    """Fold the per-model windows into one verdict.

    Corroborated by the logs where they say anything: the fallback's own warning is a `LoggerEvent` in whichever sample tripped it, and a run that logged it has demonstrated the failure rather than been predicted to have it.
    """
    missing = [
        one.model for one in windows if not one.resolved and not one.undetermined
    ]
    if missing:
        warned = " and the run logged the fallback" if _warned(samples) else ""
        return Check(
            CONTEXT_WINDOW,
            Verdict.FAILED,
            f"no context window for {', '.join(missing)}{warned} — the run assumes "
            f"128000 and stops shrinking oversized tool output",
        )
    if undetermined := [one for one in windows if one.undetermined]:
        first = undetermined[0]
        return Check(
            CONTEXT_WINDOW,
            Verdict.UNDETERMINED,
            f"could not resolve {first.model} here: {first.undetermined}",
        )
    if not windows:
        return Check(CONTEXT_WINDOW, Verdict.UNEXERCISED, "no models to check")
    aliased = [one for one in windows if one.aliased]
    detail = ", ".join(f"{one.model} {one.tokens}" for one in windows)
    if aliased:
        detail += f" ({len(aliased)} by alias)"
    return Check(CONTEXT_WINDOW, Verdict.PASSED, detail)


def _warned(samples: Sequence[EvalSample]) -> bool:
    """Whether any sample logged the context-window fallback."""
    return any(
        _FALLBACK_WARNING in getattr(getattr(event, "message", None), "message", "")
        for sample in samples
        for event in (sample.events or [])
    )


def _reasoning(samples: Sequence[EvalSample]) -> tuple[Check, Check]:
    """Whether reasoning survived to the next turn, at the model layer and on the wire.

    **Two observation points because they can disagree, and only the second is conclusive.** `ModelEvent.input` is recorded *after* `resolve_reasoning_history` has run, so it says what Inspect decided to send — the whole answer when the question is whether a `reasoning_history` setting is dropping content. It is not the whole answer when the question is whether the model got it: a provider that cannot match a reasoning block back to its original API object degrades it to plain text, which is indistinguishable at the model layer and plainly visible in the request body. That second case is what *look at the model api calls directly* means, and it is why the smoke asks for every call rather than the first five.
    """
    replayed = dropped = 0
    for sample in samples:
        events = [
            event for event in (sample.events or []) if isinstance(event, ModelEvent)
        ]
        for before, after in zip(events, events[1:], strict=False):
            produced = before.output.message
            if not _reasons(produced.content):
                continue
            carried = _carried(after.input, produced)
            if carried is None:
                # the turn is not in the next request at all -- a fresh
                # conversation rather than a stripped one, and neither answer
                continue
            replayed, dropped = (
                (replayed + 1, dropped) if carried else (replayed, dropped + 1)
            )

    if dropped:
        layer = Check(
            REASONING,
            Verdict.FAILED,
            f"{dropped} of {dropped + replayed} reasoning turns were not replayed "
            f"to the model — check `reasoning_history`",
        )
    elif replayed:
        layer = Check(REASONING, Verdict.PASSED, f"{replayed} reasoning turns replayed")
    else:
        layer = Check(
            REASONING, Verdict.UNEXERCISED, "no reasoning to replay in this rehearsal"
        )
    return layer, _reasoning_api(samples, exercised=bool(replayed))


def _reasoning_api(samples: Sequence[EvalSample], *, exercised: bool) -> Check:
    """Whether the reasoning Inspect sent actually reached the provider's request body."""
    if not exercised:
        return Check(
            REASONING_API, Verdict.UNEXERCISED, "no reasoning reached a second turn"
        )
    short = seen = 0
    for sample in samples:
        for event in sample.events or []:
            if not isinstance(event, ModelEvent) or event.call is None:
                continue
            if not (expected := _expected(event.input)):
                continue
            seen += 1
            short += 1 if _markers(event.call.request) < expected else 0
    if not seen:
        return Check(
            REASONING_API,
            Verdict.UNDETERMINED,
            "no request bodies were recorded for a turn carrying reasoning",
        )
    if short:
        return Check(
            REASONING_API,
            Verdict.FAILED,
            f"{short} of {seen} requests reached the provider carrying less "
            f"reasoning than the conversation held",
        )
    return Check(REASONING_API, Verdict.PASSED, f"{seen} requests carried it")


def _expected(messages: Sequence[ChatMessage]) -> int:
    """How many turns of this request are supposed to arrive carrying reasoning.

    Counted per *turn* rather than per block, which is what makes the comparison against a body safe in the direction that matters: a provider may fold one turn's several reasoning blocks into a single wire object, so counting blocks would fail a request that dropped nothing. A replayed turn contributes at least one marker, so a body holding fewer markers than there are reasoning turns has lost one.
    """
    return sum(1 for message in messages if _reasons(message.content))


def _reasons(content: object) -> bool:
    """Whether a message's content carries a reasoning block."""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, ContentReasoning) for item in cast(list[object], content)
    )


def _carried(
    messages: Sequence[ChatMessage], produced: ChatMessageAssistant
) -> bool | None:
    """Whether the turn `produced` is in this request, and whether it kept its reasoning.

    **Two ways of finding it, and needing the second is itself the finding.** The id is the exact identity and is tried first. But `resolve_reasoning_history` gives a filtered message a **fresh** id, so a stripped turn is precisely the case the id lookup cannot find — matching on identity alone would silently skip the failure this check exists for. So a miss falls back to the turn's content with reasoning ignored, which a strip leaves untouched: same text, same tool calls, no reasoning block, new id.

    A turn found by neither is genuinely absent — a conversation that restarted rather than one that was filtered — and is not evidence either way.

    Returns:
        `True` replayed, `False` stripped, `None` where this turn is not in the request at all.
    """
    for message in messages:
        if produced.id is not None and message.id == produced.id:
            return _reasons(message.content)
    if not (fingerprint := _fingerprint(produced)):
        # a reasoning-only turn has nothing left once reasoning is ignored, so
        # every stripped message would match it. The id was the only identity
        # available and it did not find one
        return None
    for message in messages:
        if isinstance(message, ChatMessageAssistant) and (
            _fingerprint(message) == fingerprint
        ):
            return _reasons(message.content)
    return None


def _fingerprint(message: ChatMessageAssistant) -> tuple[str, ...]:
    """What an assistant turn is, with its reasoning ignored — the part a strip preserves."""
    content = message.content
    texts = (
        tuple(
            item.text
            for item in cast(list[object], content)
            if isinstance(item, ContentText)
        )
        if isinstance(content, list)
        else (content,)
    )
    calls = tuple(one.id for one in message.tool_calls or ())
    return tuple(part for part in (*texts, *calls) if part)


def _markers(request: Mapping[str, Any]) -> int:
    """How many replayed reasoning blocks a raw provider request body carries.

    **A count rather than a yes, because a yes could not see the case that matters.** Asking only *does this body contain reasoning* passes a request in which the provider dropped the newest block and kept an older one — which is the exact shape of the conversion bug this check exists for, and the more turns a sample runs the more likely it is that something is left to say yes on. The caller compares against how many turns of the conversation were supposed to arrive carrying it.

    **A union across providers rather than a lookup keyed on one**, because every shape below is specific enough that a body containing it is carrying reasoning whoever sent it — and a smoke that had to know the provider first would be one more place for the two to disagree.

    What is deliberately *not* matched is a bare `reasoning` key: on OpenAI that is the request's reasoning *configuration* (`{"effort": "high"}`), which every reasoning request carries and which says nothing about replay.
    """
    return _walk(request)


def _walk(node: object) -> int:
    """Reasoning markers at or under this node.

    A node that *is* a marker counts one and is not descended into, so a thinking block's own signature string cannot be counted a second time underneath it.
    """
    if isinstance(node, dict):
        entry = cast(dict[str, object], node)
        kind = entry.get("type")
        if kind == "thinking" and entry.get("signature"):
            return 1  # anthropic
        if kind == "redacted_thinking" and entry.get("data"):
            return 1  # anthropic, redacted
        if kind == "reasoning":
            return 1  # openai responses -- an input item, not the config
        if entry.get("thought") is True or entry.get("thought_signature"):
            return 1  # google
        if entry.get("reasoning_details") or entry.get("reasoning_content"):
            return 1  # openrouter and the completions-compatible providers
        return sum(_walk(value) for value in entry.values())
    if isinstance(node, list):
        return sum(_walk(item) for item in cast(list[object], node))
    # the completions path smuggles reasoning into assistant text as a tag,
    # which is the only shape that is a substring rather than a structure
    return node.count("<think") if isinstance(node, str) else 0


def _reason(ex: Exception) -> str:
    """One line from an exception, short enough to sit in a table cell."""
    text = " ".join(str(ex).split())
    return text[:120] if text else type(ex).__name__


__all__ = [
    "CHECKS",
    "CONTEXT_WINDOW",
    "REASONING",
    "REASONING_API",
    "SCAN_COVERAGE",
    "Check",
    "Probe",
    "Verdict",
    "Window",
    "probe",
    "scan_coverage",
    "window",
]
