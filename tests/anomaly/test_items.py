"""Anomaly items, driven through real turns.

Layer 1 like the rest of the item suite: the errored samples are in real logs, the windows come out of the real fold over the real journal, and what is asserted is what a reader would see. The properties that matter: owner follows state, the id carries the generation and the weight bucket (the re-notification policy), one live proposal is one consolidated item, the signoff gate holds while anything is open, and a second turn journals nothing new.
"""

from pathlib import Path
from typing import Any

from inspect_steward._tend import Level, Owner, status, turn_post
from inspect_steward._tend.items import ANOMALY, SIGNOFF_READY, Item
from inspect_steward._workspace import (
    INSTANCE,
    INVESTIGATING,
    OPENED,
    PAUSED,
    PROPOSAL,
    RESOLUTION,
    RULING,
    Workspace,
    append_event,
    read_journal,
)

from .._logs import SynthSample, SynthTask, write_log
from ..schedule.test_tend import observations, prepared, turn

TIMEOUT_TRACEBACK = """Traceback (most recent call last):
  File "/venv/lib/python3.13/site-packages/openai/_client.py", line 88, in post
    raise APITimeoutError(request=request)
openai.APITimeoutError: Request timed out.
"""

CLASS = "error:openai.APITimeoutError@openai/_client.py:post"


def errored(id: str) -> SynthSample:
    return SynthSample(
        id=id, error=f"APITimeoutError('{id}')", traceback=TIMEOUT_TRACEBACK
    )


def erroring(root: Path, *, errors: int = 2, samples: int = 4) -> Workspace:
    """A one-task run whose log carries real errored samples."""
    task = SynthTask("probe", samples=samples)
    workspace, _ = prepared(root, [task])
    write_log(
        workspace.logs,
        task,
        completed=samples - errors,
        samples=[errored(f"s{n}") for n in range(errors)],
    )
    return workspace


def anomaly_items(workspace: Workspace) -> list[Item]:
    return [item for item in turn(workspace).items if item.kind == ANOMALY]


def ruling(workspace: Workspace, disposition: str, **fields: Any) -> None:
    payload: dict[str, Any] = {
        "class": CLASS,
        "disposition": disposition,
        "reason": "decided",
        "by": "kaia",
        **fields,
    }
    append_event(workspace.journal, RULING, **payload)


def test_an_open_window_is_the_agents_attention(tmp_path: Path) -> None:
    workspace = erroring(tmp_path)

    items = anomaly_items(workspace)

    assert len(items) == 1
    item = items[0]
    assert item.owner is Owner.AGENT
    assert item.level is Level.ATTENTION
    assert item.subject == CLASS
    assert item.id == f"anomaly:openai.APITimeoutError:{_digest8(CLASS)}:g1"
    assert "2 samples errored the same way" in item.summary
    assert item.action == f"steward investigate '{CLASS}'"
    assert not item.acknowledgeable


def test_a_second_turn_journals_nothing_new(tmp_path: Path) -> None:
    workspace = erroring(tmp_path)
    turn(workspace)
    before = _anomaly_events(workspace)

    result = turn(workspace)

    assert _anomaly_events(workspace) == before
    assert [entry["class"] for entry in before] == [CLASS, CLASS]
    # and the observation records the open window as a time series point
    assert observations(workspace)[-1]["anomalies"] == {CLASS: 2}
    assert result.anomalies.open[0].evidence.count == 2


def test_the_weight_rides_in_an_order_of_magnitude_bucket(tmp_path: Path) -> None:
    workspace = erroring(tmp_path, errors=12, samples=20)

    items = anomaly_items(workspace)

    assert items[0].id.endswith(":g1:x10")


def test_investigating_is_the_agents_information(tmp_path: Path) -> None:
    workspace = erroring(tmp_path)
    turn(workspace)
    fields: dict[str, Any] = {"class": CLASS, "by": "agent", "note": "reading logs"}
    append_event(workspace.journal, INVESTIGATING, **fields)

    items = anomaly_items(workspace)

    assert len(items) == 1
    assert items[0].owner is Owner.AGENT
    assert items[0].level is Level.INFO
    assert items[0].id.endswith(":investigating")
    assert "reading logs" in items[0].summary


def test_a_live_proposal_is_one_consolidated_human_item(tmp_path: Path) -> None:
    workspace = erroring(tmp_path)
    turn(workspace)
    fields: dict[str, Any] = {
        "id": "prop-abcd1234",
        "action": "rerun",
        "classes": {CLASS: {"count": 2}},
        "reason": "transient",
        "by": "agent",
    }
    append_event(workspace.journal, PROPOSAL, **fields)

    items = anomaly_items(workspace)

    assert len(items) == 1
    item = items[0]
    assert item.owner is Owner.HUMAN
    assert item.id == "anomaly:prop:prop-abcd1234"
    assert item.action == "steward rule --proposal prop-abcd1234"
    # the covered class is suppressed under it, so the class key appears in
    # the proposal's own summary and nowhere else
    assert CLASS in item.summary


def test_a_proposed_population_crossing_a_magnitude_changes_the_item(
    tmp_path: Path,
) -> None:
    # the consolidated item carries the weight bucket like a window item does,
    # so growth past 10/100/1000 re-arms the appeared-diff while the question
    # is already in front of a person
    workspace = erroring(tmp_path, errors=12, samples=20)
    turn(workspace)
    fields: dict[str, Any] = {
        "id": "prop-abcd1234",
        "action": "rerun",
        "classes": {CLASS: {"count": 12}},
        "reason": "transient",
        "by": "agent",
    }
    append_event(workspace.journal, PROPOSAL, **fields)

    items = anomaly_items(workspace)

    assert items[0].id == "anomaly:prop:prop-abcd1234:x10"


def test_a_pending_rerun_is_machinery_and_not_an_item(tmp_path: Path) -> None:
    # the tend applies the ruling itself, so a RULED window is work in motion
    # rather than anybody's decision -- the anomalies block still reports it
    # as awaiting the re-run, and only a failed outcome makes an item again
    workspace = erroring(tmp_path)
    turn(workspace)
    ruling(workspace, "rerun")

    assert anomaly_items(workspace) == []


def test_a_failed_rerun_is_one_human_review_item(tmp_path: Path) -> None:
    # the outcome has been observed, so the pending-outcome item would sit
    # beside the review contradicting it -- the review is the whole story
    workspace = erroring(tmp_path)
    turn(workspace)
    ruling(workspace, "rerun")
    outcome: dict[str, Any] = {
        "class": CLASS,
        "outcome": "reran_failed",
        "detail": "1 of 2 failed again after the re-run",
    }
    append_event(workspace.journal, RESOLUTION, **outcome)

    items = anomaly_items(workspace)

    assert len(items) == 1
    item = items[0]
    assert item.owner is Owner.HUMAN
    assert item.level is Level.ATTENTION
    assert item.id.endswith(":failed1")
    assert "did not hold" in item.summary


def test_signoff_holds_while_a_window_is_open_and_returns_on_a_ruling(
    tmp_path: Path,
) -> None:
    workspace = erroring(tmp_path)

    kinds = {item.kind for item in turn(workspace).items}
    assert ANOMALY in kinds
    assert SIGNOFF_READY not in kinds

    ruling(workspace, "dismiss")
    kinds = {item.kind for item in turn(workspace).items}
    assert ANOMALY not in kinds
    assert SIGNOFF_READY in kinds


def test_an_accepting_ruling_keeps_its_effect_on_the_record(tmp_path: Path) -> None:
    from inspect_steward._tend import status_markdown

    workspace = erroring(tmp_path)
    turn(workspace)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")

    result = status(workspace)

    accepted = result.anomalies.accepted()
    assert len(accepted) == 1
    assert accepted[0].effect == "2 samples excluded from scoring"
    assert [item for item in result.items if item.kind == ANOMALY] == []
    # the mark survives into the human-facing report, not just the fold: the
    # anomalies block carries the effect sentence, and the history carries
    # the ruling that put it there
    document = status_markdown(result)
    assert "### anomalies" in document
    assert "2 samples excluded from scoring" in document
    assert "exclude by kaia" in document


def test_an_operator_limit_window_waits_for_adjudication(tmp_path: Path) -> None:
    # the operator knows what they did: the window opens on the record and in
    # the status block, but asks nothing inline and does not hold back the
    # signoff invitation -- it gets its ruling in that conversation
    task = SynthTask("probe", samples=3)
    workspace, _ = prepared(tmp_path, [task])
    write_log(
        workspace.logs,
        task,
        samples=[
            SynthSample(id="s1", limit="operator", limit_reason="looked wrong"),
            SynthSample(id="s2", score=1.0),
            SynthSample(id="s3", score=1.0),
        ],
    )

    result = turn(workspace)

    assert [a.class_key for a in result.anomalies.open] == ["limit:operator"]
    kinds = {item.kind for item in result.items}
    assert ANOMALY not in kinds
    assert SIGNOFF_READY in kinds
    from inspect_steward._tend import status_markdown

    assert "limit:operator" in status_markdown(result)


def test_a_task_window_does_not_escalate_to_an_unattended_channel(
    tmp_path: Path,
) -> None:
    # agentless, paused so nothing spawns: the window is real and the item is
    # the agent's -- and the no-agent escalation leaves it alone, because
    # Steward's own respawn is the resolution and `stalled` is the durable news
    done, waiting = SynthTask("done"), SynthTask("waiting")
    workspace, _ = prepared(tmp_path, [done, waiting])
    write_log(workspace.logs, done)
    append_event(workspace.journal, PAUSED, by="human", reason="hold")
    window: dict[str, Any] = {"class": "task:no-log", "kind": "task"}
    append_event(workspace.journal, OPENED, **window)
    batch: dict[str, Any] = {
        "class": "task:no-log",
        "count": 1,
        "refs": [f"{waiting.identifier}@w1"],
        "tasks": [waiting.identifier],
    }
    append_event(workspace.journal, INSTANCE, **batch)

    result = turn(workspace)

    assert any(item.kind == ANOMALY for item in result.items)
    assert turn_post(result) is None


def test_an_error_window_does_escalate_when_nobody_collects(tmp_path: Path) -> None:
    # the counterpart: an errored-sample class has no mechanical exit, so an
    # unattended workspace's channel is the only reader left
    workspace = erroring(tmp_path)

    post = turn_post(turn(workspace))

    assert post is not None
    assert any(CLASS in line for line in post.lines)


def test_the_status_document_carries_the_anomalies_block(tmp_path: Path) -> None:
    from inspect_steward._tend import status_markdown

    workspace = erroring(tmp_path)
    turn(workspace)

    document = status_markdown(status(workspace))

    assert "### anomalies" in document
    assert "anomalies: 1 open" in document
    assert CLASS in document
    # and a clean run carries no empty heading
    clean, _ = prepared(tmp_path / "clean", [SynthTask("fine")])
    write_log(clean.logs, SynthTask("fine"))
    assert "### anomalies" not in status_markdown(status(clean))


def _anomaly_events(workspace: Workspace) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type in (OPENED, INSTANCE)
    ]


def _digest8(value: str) -> str:
    from hashlib import sha256

    return sha256(value.encode("utf-8")).hexdigest()[:8]
