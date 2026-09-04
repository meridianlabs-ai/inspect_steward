"""What a rehearsal's own transcripts are asked, and what each answer blocks.

Layer 1 throughout: `probe` takes logs and returns verdicts, so every case here is a transcript composed by hand. The three states that matter are *passed*, *failed*, and the two that must never be confused with failure — `unexercised` (there was nothing to check) and `undetermined` (the check could not run here). A smoke that conflated either with a failure would block every non-reasoning eval, or every machine without the provider SDK installed, on the day it shipped.
"""

from typing import Any

import pytest
from inspect_ai.event import Event, ModelEvent
from inspect_ai.log import EvalConfig, EvalDataset, EvalLog, EvalSample, EvalSpec
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageUser,
    ContentReasoning,
    ContentText,
    GenerateConfig,
    ModelOutput,
)
from inspect_ai.model._model_call import ModelCall
from inspect_steward._smoke import (
    CONTEXT_WINDOW,
    REASONING,
    REASONING_API,
    Verdict,
    probe,
    window,
)
from inspect_steward._smoke import checks as checks_module
from pydantic import JsonValue

MODEL = "mockllm/model"


class Aliasing:
    """A model that reports a different name for the window lookup than for itself.

    Which is what a provider does for an unregistered frontier name, and the only way to exercise it here: the aliasing lives on the provider class, and this venv can instantiate none of them. The lookup underneath is the real one — `get_model_input_tokens` reads the shipped database — so what is faked is the name, not the answer.
    """

    def __init__(self, name: str, alias: str) -> None:
        self._name = name
        self._alias = alias

    def canonical_name(self) -> str:
        return self._name

    def input_tokens_name(self) -> str:
        return self._alias

    def __str__(self) -> str:
        return self._name


def aliasing(monkeypatch: pytest.MonkeyPatch, alias: str | None) -> None:
    """Make every model resolve to one that aliases onto `alias`, or onto itself."""

    def resolve(name: str) -> Any:
        return Aliasing(name, alias or name)

    monkeypatch.setattr(checks_module, "get_model", resolve)


def reasoning(text: str = "let me think") -> ContentReasoning:
    return ContentReasoning(reasoning=text, signature="sig-1")


def assistant(
    identifier: str, *, thinking: bool, text: str = "answer"
) -> ChatMessageAssistant:
    """One assistant turn, with or without the reasoning block it produced."""
    content: list[Any] = [ContentText(text=text)]
    if thinking:
        content.insert(0, reasoning())
    return ChatMessageAssistant(id=identifier, content=content, model=MODEL)


def call(*, carries: bool) -> ModelCall:
    """A raw provider body, in Anthropic's shape, with or without the thinking block."""
    content: list[JsonValue] = [{"type": "text", "text": "answer"}]
    if carries:
        content.insert(0, {"type": "thinking", "thinking": "…", "signature": "sig-1"})
    request: dict[str, JsonValue] = {
        "model": MODEL,
        "messages": [{"role": "assistant", "content": content}],
    }
    return ModelCall(request=request, response={})


def model_event(
    *,
    input: list[ChatMessage],
    produced: ChatMessageAssistant,
    raw: ModelCall | None = None,
) -> ModelEvent:
    """One model call: what went in, what came back, and the body that was posted."""
    output = ModelOutput.from_content(MODEL, "answer")
    output.choices[0].message = produced
    return ModelEvent(
        model=MODEL,
        input=input,
        tools=[],
        tool_choice="none",
        config=GenerateConfig(),
        output=output,
        call=raw,
    )


def sample(*events: Event, identifier: int = 1) -> EvalSample:
    return EvalSample(
        id=identifier,
        epoch=1,
        input="q",
        target="a",
        events=list(events),
    )


def log(*samples: EvalSample) -> EvalLog:
    return EvalLog(
        eval=EvalSpec(
            created="2026-09-01T00:00:00+00:00",
            task="probe",
            dataset=EvalDataset(),
            model=MODEL,
            config=EvalConfig(),
        ),
        samples=list(samples),
    )


def check(result: Any, name: str) -> Any:
    return next(one for one in result.checks if one.name == name)


def two_turns(*, replayed: bool, wire: bool | None = None) -> EvalSample:
    """A sample whose first turn reasoned and whose second either carried it or did not.

    `wire` composes the raw body independently of the conversation, which is the whole point of the second check: a provider can drop on the wire what the model layer kept.
    """
    first = assistant("m1", thinking=True)
    second_input: list[ChatMessage] = [
        ChatMessageUser(content="q"),
        first if replayed else assistant("m2-fresh", thinking=False),
    ]
    raw = None if wire is None else call(carries=wire)
    return sample(
        model_event(input=[ChatMessageUser(content="q")], produced=first),
        model_event(
            input=second_input, produced=assistant("m3", thinking=False), raw=raw
        ),
    )


def _blocks(count: int) -> ModelCall:
    """A raw body holding exactly `count` thinking blocks."""
    content: list[JsonValue] = [
        {"type": "thinking", "thinking": "…", "signature": f"sig-{one}"}
        for one in range(count)
    ]
    content.append({"type": "text", "text": "answer"})
    request: dict[str, JsonValue] = {
        "model": MODEL,
        "messages": [{"role": "assistant", "content": content}],
    }
    return ModelCall(request=request, response={})


def _three_turns(*, kept: int) -> EvalSample:
    """Two reasoning turns replayed in the conversation, `kept` of them on the wire."""
    first = assistant("m1", thinking=True, text="one")
    second = assistant("m2", thinking=True, text="two")
    return sample(
        model_event(input=[ChatMessageUser(content="q")], produced=first),
        model_event(input=[ChatMessageUser(content="q"), first], produced=second),
        model_event(
            input=[ChatMessageUser(content="q"), first, second],
            produced=assistant("m3", thinking=False),
            raw=_blocks(kept),
        ),
    )


class TestTheContextWindow:
    """*Did the model resolve to a window*, which is not *is it in the database*.

    Providers alias an unknown frontier name onto a known entry before any lookup, so a pre-deployment codename with no entry of its own still gets a real window. The check is on the number the runtime ends up with, because the alternative is 128000 assumed silently.
    """

    def test_a_model_that_resolves_passes_and_reports_its_window(self) -> None:
        result = probe([log(sample())], models=[MODEL])

        entry = check(result, CONTEXT_WINDOW)
        assert entry.verdict is Verdict.PASSED
        assert "128000" in entry.detail

    def test_mockllm_passes_on_the_merits_rather_than_by_exemption(self) -> None:
        # it returns the default window *deliberately*, at a different line from
        # the fallback -- so a check written as "is it 128000" would fail it and
        # a check written as "did it resolve" is right about it for free
        assert window(MODEL).tokens == 128000
        assert window(MODEL).undetermined is None

    def test_a_model_this_interpreter_cannot_resolve_is_undetermined_not_failed(
        self,
    ) -> None:
        # a provider Steward's interpreter cannot load says nothing about the
        # worker's, so this reports and never blocks. `get_model` raises here
        # rather than returning nothing, which is what keeps *could not check*
        # separable from *checked, and it is wrong*.
        #
        # **A name no provider claims, rather than a real one whose SDK is
        # absent.** This read `openai/gpt-5` and passed only where the openai
        # package happened not to be installed — which is not the machine that
        # runs openai evals, so `pytest` was red for everybody working on this
        # repo and green in CI. Which extras a developer has is not the
        # subject; what `window` does when the call raises is
        result = probe([log(sample())], models=["nosuchprovider/gpt-5"])

        entry = check(result, CONTEXT_WINDOW)
        assert entry.verdict is Verdict.UNDETERMINED
        assert not entry.blocks

    def test_an_unregistered_name_aliased_onto_a_frontier_entry_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # **the pre-deployment case, and the one a `get_model_info` check would
        # have failed.** The codename has no database entry of its own; the
        # provider aliases it onto the current frontier before any lookup, so
        # the run gets a real window. That is fine — and which window it got is
        # the sentence worth printing, so the alias is named rather than hidden
        aliasing(monkeypatch, "openai/gpt-5")

        result = probe([log(sample())], models=["openai/frobnicator-preview"])

        entry = check(result, CONTEXT_WINDOW)
        assert entry.verdict is Verdict.PASSED
        assert "by alias" in entry.detail
        (resolved,) = result.windows
        assert resolved.aliased == "openai/gpt-5"
        # the window the runtime consumes, which is not `context_length`
        assert resolved.tokens == 272_000

    def test_a_name_that_aliases_onto_nothing_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # nothing to alias onto and no entry of its own: this is the run that
        # silently assumes 128000 and silently stops shrinking tool output
        aliasing(monkeypatch, None)

        result = probe([log(sample())], models=["mycorp/nothing-at-all"])

        entry = check(result, CONTEXT_WINDOW)
        assert entry.verdict is Verdict.FAILED
        assert entry.blocks
        assert "128000" in entry.detail


class TestReasoningAtTheModelLayer:
    """*Did the reasoning from turn N reach turn N+1's input.*

    `ModelEvent.input` is recorded after `resolve_reasoning_history` has run, so it is what Inspect decided to send rather than what the caller assembled.
    """

    def test_reasoning_carried_into_the_next_turn_passes(self) -> None:
        result = probe([log(two_turns(replayed=True))], models=[MODEL])

        assert check(result, REASONING).verdict is Verdict.PASSED

    def test_reasoning_stripped_before_the_next_turn_fails(self) -> None:
        # the turn is gone from the request under its own id, which is what a
        # strip does -- it does not merely lose the block, it re-ids the message
        result = probe([log(two_turns(replayed=False))], models=[MODEL])

        entry = check(result, REASONING)
        assert entry.verdict is Verdict.FAILED
        assert entry.blocks
        assert "reasoning_history" in entry.detail

    def test_a_run_with_no_reasoning_is_unexercised_and_does_not_block(self) -> None:
        # a non-reasoning model has nothing to replay, and calling that a
        # failure would block every such eval
        plain = sample(
            model_event(
                input=[ChatMessageUser(content="q")],
                produced=assistant("m1", thinking=False),
            ),
            model_event(
                input=[ChatMessageUser(content="q")],
                produced=assistant("m2", thinking=False),
            ),
        )
        result = probe([log(plain)], models=[MODEL])

        entry = check(result, REASONING)
        assert entry.verdict is Verdict.UNEXERCISED
        assert not entry.blocks

    def test_a_single_turn_sample_is_unexercised(self) -> None:
        # reasoning that never had a next turn was never owed one
        only = sample(
            model_event(
                input=[ChatMessageUser(content="q")],
                produced=assistant("m1", thinking=True),
            )
        )

        assert check(probe([log(only)], models=[MODEL]), REASONING).verdict is (
            Verdict.UNEXERCISED
        )


class TestReasoningOnTheWire:
    """*Did it reach the body that was actually posted*, which the model layer cannot say.

    This is the check that catches a provider converting a reasoning block it could not match back into plain text — identical at the model layer, plainly visible in the request.
    """

    def test_a_body_carrying_the_thinking_block_passes(self) -> None:
        result = probe([log(two_turns(replayed=True, wire=True))], models=[MODEL])

        assert check(result, REASONING_API).verdict is Verdict.PASSED

    def test_kept_in_the_conversation_and_dropped_from_the_body_fails(self) -> None:
        # the case only the raw call can see, and the reason the smoke asks for
        # every call rather than the first five
        result = probe([log(two_turns(replayed=True, wire=False))], models=[MODEL])

        entry = check(result, REASONING_API)
        assert entry.verdict is Verdict.FAILED
        assert entry.blocks

    def test_no_recorded_body_is_undetermined_not_failed(self) -> None:
        result = probe([log(two_turns(replayed=True, wire=None))], models=[MODEL])

        entry = check(result, REASONING_API)
        assert entry.verdict is Verdict.UNDETERMINED
        assert not entry.blocks

    def test_nothing_to_replay_leaves_the_wire_check_unexercised(self) -> None:
        result = probe([log(two_turns(replayed=False, wire=False))], models=[MODEL])

        assert check(result, REASONING_API).verdict is Verdict.UNEXERCISED

    def test_the_newest_block_dropped_beside_an_older_one_kept_fails(self) -> None:
        """**Counted rather than asked, which is what makes this visible at all.**

        A body carrying *some* reasoning answers yes to *does this carry
        reasoning*, so a provider dropping the newest block while retaining an
        older one passed — and the more turns a sample runs the more likely it
        is that something is left to say yes on. The comparison is now between
        how many turns of the conversation were supposed to arrive carrying it
        and how many markers the body holds.
        """
        result = probe([log(_three_turns(kept=1))], models=[MODEL])

        entry = check(result, REASONING_API)
        assert entry.verdict is Verdict.FAILED
        assert entry.blocks

    def test_every_block_carried_still_passes(self) -> None:
        # the same shape with nothing dropped, so the failure above is the drop
        # rather than the counting
        result = probe([log(_three_turns(kept=2))], models=[MODEL])

        assert check(result, REASONING_API).verdict is Verdict.PASSED
