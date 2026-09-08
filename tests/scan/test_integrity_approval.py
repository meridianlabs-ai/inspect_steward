"""`scoring_integrity` must not run its own tool calls through the eval's approval policy.

A scanner reviews recorded transcripts; it is never the agent under evaluation. But its
structured-output `answer` tool call still flows through inspect's
`execute_tools` -> `apply_tool_approval`, and online scanning shares the per-sample context of
the `eval_set(approval=...)` it accompanies. Without suspension, an escape-guard-style approver
active in that context judges the scanner's own `answer` call, fails closed with no active
sample, and its fail-streak breaker terminates the scan
(`TerminateSampleError: Tool call approver requested termination.`) — the failure this guards.

`scoring_integrity` wraps each review in an approve-all approval context (`_SCAN_APPROVAL`), so
a terminating approver in the surrounding context must be invisible to the scanner's tool call.
"""

import asyncio

import inspect_steward._scan.integrity as integ
import pytest
from inspect_ai.approval import ApprovalPolicy, approval, auto_approver
from inspect_ai.approval._apply import apply_tool_approval
from inspect_ai.tool import ToolCall
from inspect_scout import Result, Scanner, Transcript
from inspect_steward._scan.integrity import scoring_integrity


async def _review(scan: "Scanner[Transcript]", transcript: Transcript) -> object:
    """Await one scan, so `asyncio.run` has a `Coroutine` to drive (cf. test_integrity_binding)."""
    return await scan(transcript)


def _stub_llm_scanner_that_makes_a_tool_call(
    monkeypatch: pytest.MonkeyPatch, seen: dict[str, object]
) -> None:
    """Replace `llm_scanner` with one whose scan runs a tool call through inspect's approval.

    `apply_tool_approval` is the exact function `execute_tools`/`call_tools` consult, so this
    records the decision the scanner's `answer` call would actually receive.
    """

    def fake_llm_scanner(*, question: object, answer: object, model: object) -> object:
        async def scan(_: Transcript) -> Result:
            approved, approval_ = await apply_tool_approval(
                "review", ToolCall(id="c0", function="answer", arguments={}), None, []
            )
            seen["approved"] = approved
            seen["decision"] = approval_.decision if approval_ else None
            return Result(value=False, answer="ok", explanation="x")

        return scan

    monkeypatch.setattr(integ, "llm_scanner", fake_llm_scanner)


def test_a_terminating_approver_does_not_reach_the_scanner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    _stub_llm_scanner_that_makes_a_tool_call(monkeypatch, seen)
    scan = scoring_integrity(model="explicit/model")
    transcript = Transcript.model_validate({"transcript_id": "t1"})

    # Stand in for the eval's active escape guard: terminate every tool call.
    with approval([ApprovalPolicy(auto_approver("terminate"), tools="*")]):
        asyncio.run(_review(scan, transcript))

    # Suspended: the scanner's own call is auto-approved, not terminated.
    assert seen["approved"] is True
    assert seen["decision"] == "approve"


def test_the_outer_approver_is_restored_after_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the suspension is scoped to the review; it must not leak out and disarm the guard
    seen: dict[str, object] = {}
    _stub_llm_scanner_that_makes_a_tool_call(monkeypatch, seen)
    scan = scoring_integrity(model="explicit/model")
    transcript = Transcript.model_validate({"transcript_id": "t1"})

    async def drive() -> object:
        with approval([ApprovalPolicy(auto_approver("terminate"), tools="*")]):
            await _review(scan, transcript)
            # after the review returns, the surrounding terminating approver is back in force
            _, approval_ = await apply_tool_approval(
                "after", ToolCall(id="c1", function="bash", arguments={}), None, []
            )
            return approval_.decision if approval_ else None

    assert asyncio.run(drive()) == "terminate"
