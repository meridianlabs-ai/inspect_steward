"""How `scoring_integrity` chooses the model that reviews each transcript.

The scanner is injected on every run with no model, so by default it falls to the sample's own model
under evaluation — a transcript judged by the model that produced it. A per-sample resolver
(`set_scan_model_resolver`) is how a caller (veevals' `campaign(judge=…)`) points it at a declared
judge instead, resolved against each transcript's own vendor. These pin that the resolver is
consulted, memoised, and never worsens the model-less or no-resolver case.
"""

import asyncio
from collections.abc import Iterator

import inspect_steward._scan.integrity as integ
import pytest
from inspect_scout import Result, Scanner, Transcript
from inspect_steward._scan import set_scan_model_resolver
from inspect_steward._scan.integrity import scoring_integrity


@pytest.fixture(autouse=True)
def reset_process_state() -> Iterator[None]:
    """The resolver is a process global; keep tests independent."""
    yield
    set_scan_model_resolver(None)


async def _review(
    scan: "Scanner[Transcript]", transcript: Transcript
) -> Result | list[Result]:
    """Await one scan as a coroutine, so `asyncio.run` has a `Coroutine` to drive."""
    return await scan(transcript)


def _record_built_models(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace `llm_scanner` with a stub that records the model each build was handed."""
    built: list[object] = []

    def fake_llm_scanner(*, question: object, answer: object, model: object) -> object:
        built.append(model)

        async def scan(_: Transcript) -> Result:
            return Result(value=False, answer="ok", explanation="x")

        return scan

    monkeypatch.setattr(integ, "llm_scanner", fake_llm_scanner)
    return built


def _transcript(model: str | None, tid: str) -> Transcript:
    fields: dict[str, object] = {"transcript_id": tid}
    if model is not None:
        fields["model"] = model
    return Transcript.model_validate(fields)


def test_resolver_binds_the_scan_model_per_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _record_built_models(monkeypatch)
    set_scan_model_resolver(
        lambda m: (
            "anthropic/judge" if m and m.startswith("anthropic") else "openai/judge"
        )
    )
    scan = scoring_integrity()  # model=None → resolver path

    asyncio.run(_review(scan, _transcript("anthropic/mimas", "t1")))
    asyncio.run(
        _review(scan, _transcript("anthropic/other", "t2"))
    )  # same judge → memoised
    asyncio.run(_review(scan, _transcript("openai/gpt", "t3")))

    # one build per distinct resolved model, not one per transcript
    assert built == ["anthropic/judge", "openai/judge"]


def test_no_resolver_is_the_ambient_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing registered the scanner builds `model=None` — the pre-resolver behaviour."""
    built = _record_built_models(monkeypatch)
    scan = scoring_integrity()

    asyncio.run(_review(scan, _transcript("anthropic/mimas", "t1")))
    asyncio.run(_review(scan, _transcript("openai/gpt", "t2")))

    assert built == [None]


def test_a_model_less_transcript_falls_back_to_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver returning None leaves the transcript on the ambient model, as if unregistered."""
    built = _record_built_models(monkeypatch)
    set_scan_model_resolver(lambda m: "anthropic/judge" if m else None)
    scan = scoring_integrity()

    asyncio.run(_review(scan, _transcript(None, "t1")))

    assert built == [None]


def test_an_explicit_model_wins_over_any_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _record_built_models(monkeypatch)
    set_scan_model_resolver(lambda m: "resolver/should-not-be-used")
    scan = scoring_integrity(model="explicit/model")

    asyncio.run(_review(scan, _transcript("anthropic/mimas", "t1")))

    assert built == ["explicit/model"]
