"""The one list a turn produces, and the verdict over it.

Layer 1 throughout, and deliberately driven through the real `tend`: an item is
a projection of what a turn saw, so constructing one by hand would test the
constructor. The conditions are synthesized in the workspace — a definition
edited after capture, a file that is not a log, a task that crashed twice — and
what is asserted is what came out the other end.

Two properties carry the design and get the most cases. **The id is the
re-notification policy**: acknowledging something must silence exactly it, and
must not silence the next one. And **the verdict is run-level**, so a finished
run with a caveat and a stuck run with the same caveat must not read alike.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from inspect_steward._tend import (
    OBSERVATION,
    Level,
    Owner,
    Verdict,
    collect_markdown,
    status,
    verdict,
    verdict_line,
)
from inspect_steward._tend.items import (
    ACTION_FAILED,
    DEGRADED,
    DRIFT,
    PARKED,
    SIGNOFF_READY,
    STALLED,
    TIMER_DRIFT,
    UNREADABLE,
    UNSUPERVISED,
    UNWRITTEN,
    Item,
)
from inspect_steward._tend.items import STUCK as STUCK_SAMPLE
from inspect_steward._worker import (
    LiveFleet,
    LiveParked,
    LiveSamples,
    LiveStuck,
    LiveTask,
    StuckSample,
)
from inspect_steward._workspace import (
    ACKNOWLEDGED,
    ARMED,
    COLLECTED,
    DISARMED,
    LAUNCHED,
    SIGNOFF,
    Workspace,
    append_event,
)

from .._acp import Publish, publish
from .._logs import DEFINITION, SynthTask, write_log
from .test_tend import prepared, turn

__all__ = ["publish"]

TASK = SynthTask("probe", samples=4)

STUCK = SynthTask("stuck")
"""Ten samples, so a log carrying four of them reads `short` rather than done."""


def items(workspace: Workspace) -> dict[str, Item]:
    """The items of one real turn, by kind."""
    return {item.kind: item for item in turn(workspace).items}


def ack(workspace: Workspace, item: Item | str, *, by: str = "operator") -> None:
    """Dispose of an item, as `steward ack` does — kind and subject included.

    The verb records both off the item it matched, and both are load-bearing
    afterwards: the kind routes a disposal into `anomalies.md`, and the subject
    is what says which task a stall was about.
    """
    fields: dict[str, Any] = (
        {
            "id": item.id,
            "kind": item.kind,
            "subject": item.subject,
            "summary": item.summary,
        }
        if isinstance(item, Item)
        else {"id": item}
    )
    append_event(workspace.journal, ACKNOWLEDGED, **fields, by=by, reason="because")


def sign(workspace: Workspace, item: Item) -> None:
    """Accept the results, as `steward signoff` records it.

    The digest is taken from the item's own subject, which is what the verb
    reads off the turn it ran — so a test that signs what it was shown cannot
    accidentally sign a different task set.
    """
    append_event(
        workspace.journal,
        SIGNOFF,
        by="kaia",
        note="",
        digest=item.subject,
        exceptions=[],
    )


def stalling(workspace: Workspace, attempts: int, *, first: int = 10) -> None:
    """Land `attempts` logs for `STUCK` that all got exactly as far as each other.

    The log-visible half of the stall guard, which needs no processes at all —
    the crash-loop half does, and this file is not about the guard. Four samples
    of the ten the manifest asks for, the same four every time.
    """
    for hour in range(attempts):
        write_log(
            workspace.logs,
            STUCK,
            total=4,
            completed=4,
            created=f"2026-08-23T{first + hour:02d}:00:00+00:00",
        )


# --- what a condition projects to ---------------------------------------


def test_a_definition_edited_after_capture_is_the_human_s(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.root / DEFINITION).write_bytes(b"# edited\n")

    item = items(workspace)[DRIFT]

    assert item.owner is Owner.OPERATOR
    assert item.level is Level.ATTENTION
    assert item.action == "steward launch"
    assert item.acknowledgeable


def test_a_file_that_is_not_a_log_is_the_agent_s(tmp_path: Path) -> None:
    # nothing here needs a decision -- somebody has to go and look, which is
    # investigation rather than a question
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")

    item = items(workspace)[UNREADABLE]

    assert item.owner is Owner.AGENT
    assert item.id == "unreadable:broken.eval"
    # the location is an absolute URI; the line an operator reads is not
    assert item.subject.endswith("broken.eval") and item.subject != item.id


def test_an_item_names_its_task_readably_and_pins_it_exactly(tmp_path: Path) -> None:
    # a task identifier is ~200 characters with two sha256s in it. An id has to
    # be typed, so it carries the name and eight hex of the identifier
    workspace, _ = prepared(tmp_path, [STUCK])
    stalling(workspace, 3)

    item = items(workspace)[STALLED]

    assert item.id.startswith("stalled:stuck:")
    assert len(item.id) < 40
    assert item.subject not in item.id
    assert "stuck" in item.summary


def test_an_action_that_could_not_be_carried_out_is_the_agent_s(
    tmp_path: Path,
) -> None:
    # a real failure rather than a constructed one: an orphan to archive, and
    # a `logs-archive` that is a file, so the move cannot happen
    orphan = SynthTask("removed")
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    write_log(workspace.logs, orphan)
    workspace.logs_archive.write_bytes(b"in the way")

    item = items(workspace)[ACTION_FAILED]

    assert item.owner is Owner.AGENT
    assert item.level is Level.ATTENTION
    # and it is the one kind nothing can dispose of: a single-turn fact, where
    # acknowledging would silence a recurrence rather than an item
    assert not item.acknowledgeable
    assert "_transient_" in collect_markdown(turn(workspace))


def test_a_blocked_item_does_not_by_itself_stop_a_working_run() -> None:
    """`BLOCKING` orders an item; it does not decide the run.

    One parked worker among eight is a run that is working with a decision
    inside it, and painting that red is how a reader learns to discount red.
    What stops a run is arithmetic over the fleet — see the table below.
    """
    blocked = Item(
        id="parked:1",
        kind=PARKED,
        owner=Owner.OPERATOR,
        level=Level.BLOCKING,
        subject="1",
        summary="waiting on an approval",
    )

    assert (
        verdict([blocked], paused=False, running=8, spawning=0, unfinished=4, parked=1)
        is Verdict.ATTENTION
    )


# --- a worker waiting on an operator ---------------------------------------
#
# The one condition in this file that cannot be synthesized in the workspace: a
# park lives on a running worker's socket, and there is no directory to put one
# in. So these substitute the fleet read — the single function whose whole job
# is *ask the running workers how they are getting on* — and let the real turn
# do everything else, items and verdict and the file it writes. The read itself
# is covered against a real socket in `tests/worker/test_live.py`, and the whole
# chain including the routing in the launch tests.


def parked_run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    parked: LiveParked,
    *,
    in_flight: int = 1,
) -> Workspace:
    """A run whose one worker is waiting on an operator.

    The log is written so the task is settled and the turn spawns nothing; the
    fleet then reports it as running, which is what a resumed worker looks like
    to a turn that has not caught up with its log.
    """
    workspace, manifest = prepared(root, [TASK])
    write_log(workspace.logs, TASK)
    fleet = LiveFleet(
        tasks={
            manifest.tasks[0].identifier: LiveTask(
                pid=os.getpid(),
                identifier=manifest.tasks[0].identifier,
                samples=LiveSamples(total=4, completed=0, in_flight=in_flight),
                parked=parked,
            )
        }
    )

    def read(inflight: object, logs: object, *, stuck_after: float = 0.0) -> LiveFleet:
        return fleet

    monkeypatch.setattr("inspect_steward._tend.turn._live", read)
    return workspace


def test_a_parked_worker_is_the_human_s_and_nobody_else_may_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking, operator-owned, and unacknowledgeable — each for its own reason.

    Answering an approval is authority over what the eval measures, so it is
    the one kind policy may not re-route. And it is the one operator-owned kind
    that cannot be acked: only answering clears it, so an acknowledgment would
    silence a worker still holding its slot.
    """
    workspace = parked_run(
        tmp_path, monkeypatch, LiveParked(approvals=1, functions=("bash",))
    )
    item = items(workspace)[PARKED]

    assert item.owner is Owner.OPERATOR
    assert item.level is Level.BLOCKING
    assert not item.acknowledgeable
    # ...but its id is still on screen, because `steward raise` takes it. The
    # two questions came apart here for the first time
    assert item.addressable
    assert item.id.startswith("parked:probe:")
    # and the whole line reaches the document an operator reads
    assert item.summary in workspace.status.read_text(encoding="utf-8")


def test_a_park_names_the_tool_and_not_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the arguments and an `ask_user` prompt are model-generated text, and this
    # line is relayed verbatim by an agent that then acts on it
    workspace = parked_run(
        tmp_path, monkeypatch, LiveParked(approvals=1, functions=("bash",))
    )
    item = items(workspace)[PARKED]

    assert "is waiting on an approval for bash" in item.summary
    # named in full, as every item names its task: it travels alone
    assert "probe" in item.summary


PARKS: list[tuple[str, LiveParked, str]] = [
    (
        "one approval",
        LiveParked(approvals=1, functions=("bash",)),
        "an approval for bash",
    ),
    ("one question", LiveParked(questions=1), "an answer to a question"),
    (
        "several",
        LiveParked(approvals=2, questions=1, functions=("bash", "python")),
        "3 samples waiting on an operator: 2 approvals (bash, python) and 1 question",
    ),
]


@pytest.mark.parametrize(
    ("parked", "expected"),
    [case[1:] for case in PARKS],
    ids=[case[0] for case in PARKS],
)
def test_a_park_says_what_is_being_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parked: LiveParked, expected: str
) -> None:
    workspace = parked_run(tmp_path, monkeypatch, parked, in_flight=parked.total)
    assert expected in items(workspace)[PARKED].summary


def test_a_park_keeps_its_id_while_the_same_task_is_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id is the notification edge: present while parked, gone once answered.

    Keyed on the task alone rather than on the count or the functions, so that
    answering one of three parks does not resolve the item and re-raise it as a
    different one — which is the churn step 24's appeared/resolved diff would
    have reported as a fresh decision each time.
    """
    workspace = parked_run(
        tmp_path, monkeypatch, LiveParked(approvals=2, questions=1, functions=("a",))
    )
    three = items(workspace)[PARKED]

    parked_run(tmp_path, monkeypatch, LiveParked(approvals=1, functions=("b",)))
    one = items(workspace)[PARKED]

    parked_run(tmp_path, monkeypatch, LiveParked())
    answered = turn(workspace)

    assert three.id == one.id
    # gone once somebody answered, and the turn that first misses it says so —
    # which is the edge a notification fires on (step 24)
    assert PARKED not in {item.kind for item in answered.items}
    assert three.id in answered.resolved


def test_a_park_carries_the_command_that_reaches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publish: Publish
) -> None:
    """The action is an address, not an instruction to answer.

    Steward detects the wait and hands over the socket; inspect's own client
    does the talking, because answering an approval is authority over what the
    eval measures and the agent may never take it (agent.md §6).
    """
    socket = tmp_path / "w.sock"
    publish(os.getpid(), socket)
    workspace = parked_run(tmp_path, monkeypatch, LiveParked(approvals=1))

    assert items(workspace)[PARKED].action == "inspect acp"


def test_a_park_on_a_worker_with_no_acp_server_still_reports_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the bind degrades rather than failing the eval, so a worker can be parked
    # with no address to offer. What that costs is the command, not the item —
    # somebody still owes an answer and still has to be told
    workspace = parked_run(tmp_path, monkeypatch, LiveParked(approvals=1))
    item = items(workspace)[PARKED]

    assert item.action is None
    assert "waiting on an approval" in item.summary


def test_a_park_sorts_above_the_other_decisions_a_person_owes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `BLOCKING` is precedence, and this is what it buys: the thing costing a
    # worker right now comes before the thing that can wait (agent.md §4.1)
    workspace = parked_run(tmp_path, monkeypatch, LiveParked(approvals=1))
    (workspace.root / DEFINITION).write_bytes(b"# edited\n")

    given = turn(workspace).items
    human = [item.kind for item in given if item.owner is Owner.OPERATOR]

    assert human[0] == PARKED
    assert DRIFT in human


# --- a sample that has stopped moving ------------------------------------


def wedged(function: str = "bash", *, asked: bool = False) -> LiveStuck:
    """One sample stuck on one pending tool call for two hours."""
    return LiveStuck(
        count=1,
        oldest_idle=7200.0,
        samples=(
            StuckSample(
                sample_id="s1",
                epoch=1,
                idle=7200.0,
                function=function,
                call_id="c1",
                cancel_requested=asked,
            ),
        ),
        asked=asked,
    )


def stuck_run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stuck: LiveStuck,
    *,
    task_id: str = "T1",
) -> Workspace:
    """A run whose one worker holds samples that have stopped moving."""
    workspace, manifest = prepared(root, [TASK])
    write_log(workspace.logs, TASK)
    fleet = LiveFleet(
        tasks={
            manifest.tasks[0].identifier: LiveTask(
                pid=os.getpid(),
                identifier=manifest.tasks[0].identifier,
                task_id=task_id,
                samples=LiveSamples(total=4, completed=0, in_flight=1),
                stuck=stuck,
            )
        }
    )

    def read(inflight: object, logs: object, *, stuck_after: float = 0.0) -> LiveFleet:
        return fleet

    monkeypatch.setattr("inspect_steward._tend.turn._live", read)
    return workspace


def test_a_stuck_sample_carries_the_ladder_s_first_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # nothing pre-authorized: the condition is reported, the command is ready,
    # and running it is an operator's act
    workspace = stuck_run(tmp_path, monkeypatch, wedged())
    item = items(workspace)[STUCK_SAMPLE]

    assert item.owner is Owner.OPERATOR
    assert item.level is Level.ATTENTION
    assert item.acknowledgeable
    assert item.id.startswith("stuck:probe:")
    assert not item.id.endswith(":asked")
    assert "stopped moving inside bash" in item.summary
    assert "nothing failed" in item.summary
    assert item.action == "inspect ctl sample cancel-tool-call T1 s1 1"


@pytest.mark.parametrize(
    ("granted", "function", "owner"),
    [
        pytest.param("stuck_cancel: [bash]\n", "bash", Owner.AGENT, id="named"),
        pytest.param("stuck_cancel: true\n", "bash", Owner.AGENT, id="any"),
        pytest.param("stuck_cancel: [bash]\n", "python", Owner.OPERATOR, id="unnamed"),
        pytest.param("", "bash", Owner.OPERATOR, id="ungranted"),
    ],
)
def test_the_grant_decides_who_holds_rung_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    granted: str,
    function: str,
    owner: Owner,
) -> None:
    workspace = stuck_run(tmp_path, monkeypatch, wedged(function))
    if granted:
        workspace.directives.write_text(granted, encoding="utf-8")

    assert items(workspace)[STUCK_SAMPLE].owner is owner


def test_an_unheeded_cancel_is_a_new_item_and_a_person_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `:asked` flip re-notifies through the ordinary appeared diff.

    Rung 1 has been spent, so the grant no longer covers it whatever the file
    says — the delivered-but-unheeded state escalates, and never repeats the
    ask.
    """
    workspace = stuck_run(tmp_path, monkeypatch, wedged())
    workspace.directives.write_text("stuck_cancel: [bash]\n", encoding="utf-8")
    quiet = items(workspace)[STUCK_SAMPLE]

    stuck_run(tmp_path, monkeypatch, wedged(asked=True))
    asked = items(workspace)[STUCK_SAMPLE]

    assert quiet.id != asked.id
    assert asked.id.endswith(":asked")
    assert asked.owner is Owner.OPERATOR
    assert "a cancel was asked and it did not stop" in asked.summary
    assert asked.action == "inspect ctl sample cancel T1 s1 1"


def test_two_calls_on_one_sample_stay_on_rung_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant covers cancelling a call, never the sample.

    A sample wedged on two admitted calls is still the agent's — and its
    command names one call by id, because `sample cancel` ends the whole
    sample and records an outcome, which exceeds anything `stuck_cancel`
    can authorize.
    """
    two_calls = LiveStuck(
        count=1,
        oldest_idle=7200.0,
        samples=(
            StuckSample(
                sample_id="s1", epoch=1, idle=7200.0, function="bash", call_id="c1"
            ),
            StuckSample(
                sample_id="s1", epoch=1, idle=7200.0, function="python", call_id="c2"
            ),
        ),
    )
    workspace = stuck_run(tmp_path, monkeypatch, two_calls)
    workspace.directives.write_text("stuck_cancel: true\n", encoding="utf-8")
    item = items(workspace)[STUCK_SAMPLE]

    assert item.owner is Owner.AGENT
    assert (
        item.action == "inspect ctl sample cancel-tool-call T1 s1 1 --tool-call-id c1"
    )


def test_two_stuck_samples_get_the_listing_rather_than_a_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a ladder is climbed one target at a time, so the item hands over the
    # listing and the reader picks their footing
    two = LiveStuck(
        count=2,
        oldest_idle=7200.0,
        samples=(
            StuckSample(sample_id="s1", epoch=1, idle=7200.0),
            StuckSample(sample_id="s2", epoch=1, idle=3600.0),
        ),
    )
    workspace = stuck_run(tmp_path, monkeypatch, two)
    item = items(workspace)[STUCK_SAMPLE]

    assert "2 samples that have stopped moving" in item.summary
    assert item.action == "inspect ctl sample list T1 --json"


def test_an_ack_silences_the_episode_and_not_the_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id carries the episode — the set of stuck samples — so an ack ends.

    An acknowledgment is permanent per id, and a task name is not an episode:
    keyed on the name alone, accepting one wedged `bash` in week one silenced
    every stuck sample the task would ever have.
    """
    workspace = stuck_run(tmp_path, monkeypatch, wedged())
    first = items(workspace)[STUCK_SAMPLE]
    ack(workspace, first.id)

    # the same episode stays accepted, turn after turn
    assert STUCK_SAMPLE not in items(workspace)

    different = LiveStuck(
        count=1,
        oldest_idle=3600.0,
        samples=(
            StuckSample(
                sample_id="s2", epoch=1, idle=3600.0, function="bash", call_id="c2"
            ),
        ),
    )
    stuck_run(tmp_path, monkeypatch, different)
    fresh = items(workspace)[STUCK_SAMPLE]

    assert fresh.id != first.id


def test_spending_rung_one_on_one_call_re_arms_the_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking one call of several is a new episode with a new ask.

    The digest is over the un-asked calls, so an acknowledgment of the first
    ask does not cover the second — keyed on the sample set alone, acking the
    c1 item silenced the c2 action forever.
    """

    def calls(*, first_asked: bool) -> LiveStuck:
        return LiveStuck(
            count=1,
            oldest_idle=7200.0,
            samples=(
                StuckSample(
                    sample_id="s1",
                    epoch=1,
                    idle=7200.0,
                    function="bash",
                    call_id="c1",
                    cancel_requested=first_asked,
                ),
                StuckSample(
                    sample_id="s1",
                    epoch=1,
                    idle=7200.0,
                    function="python",
                    call_id="c2",
                ),
            ),
        )

    workspace = stuck_run(tmp_path, monkeypatch, calls(first_asked=False))
    first = items(workspace)[STUCK_SAMPLE]
    assert first.action is not None and "--tool-call-id c1" in first.action
    ack(workspace, first.id)

    stuck_run(tmp_path, monkeypatch, calls(first_asked=True))
    item = items(workspace)[STUCK_SAMPLE]

    assert item.id != first.id
    assert item.action is not None and "--tool-call-id c2" in item.action


def test_a_sample_id_is_free_text_and_the_command_survives_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the action is a line the runbook tells an agent to execute, and a sample
    # id comes off the dataset -- whatever it holds must arrive at the CLI as
    # one argument
    hostile = LiveStuck(
        count=1,
        oldest_idle=7200.0,
        samples=(
            StuckSample(
                sample_id="q 1; rm -rf",
                epoch=1,
                idle=7200.0,
                function="bash",
                call_id="c1",
            ),
        ),
    )
    workspace = stuck_run(tmp_path, monkeypatch, hostile)
    item = items(workspace)[STUCK_SAMPLE]

    assert item.action == "inspect ctl sample cancel-tool-call T1 'q 1; rm -rf' 1"


def test_a_signature_survives_an_edit_that_produces_no_new_results(
    tmp_path: Path,
) -> None:
    """The signature is keyed on the manifest, not on the file on disk.

    What was accepted is a *set of results*, and results change at a launch. An
    edit sitting unlaunched in the definition changes nothing on disk — `drift`
    is what reports it — so keying the attestation on the live hash would
    re-open a settled decision every time somebody saved the file.
    """
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    sign(workspace, items(workspace)[SIGNOFF_READY])

    (workspace.root / DEFINITION).write_bytes(b"# edited, not launched\n")
    after = turn(workspace)
    kinds = {item.kind for item in after.items}

    # the edit is heard, once, as the thing it actually is
    assert DRIFT in kinds
    assert SIGNOFF_READY not in kinds
    assert after.verdict is Verdict.SIGNED_OFF


def test_signing_one_result_set_does_not_sign_a_different_one(
    tmp_path: Path,
) -> None:
    """A byte-identical definition can enumerate different tasks.

    Flow arguments live beside the file, an import can change under it, and
    `definition_hash` covers the top-level file and nothing else — so a hash of
    the definition is not an identity for a *result set*. Keying the attestation
    on it would let an old signature silently cover results nobody had looked at.
    """
    other = SynthTask("second")
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    sign(workspace, items(workspace)[SIGNOFF_READY])

    # the same definition bytes, and therefore the same content hash, enumerating
    # one more task -- which is what a Flow spec run with different arguments does
    prepared(tmp_path, [TASK, other])
    write_log(workspace.logs, other)

    assert SIGNOFF_READY in items(workspace)


def test_signing_a_short_run_does_not_sign_the_longer_one(
    tmp_path: Path,
) -> None:
    """The identifier deliberately does not move when the sample count does.

    `task_identifier` covers the solver plan, config and limits, and pointedly
    not `samples` or `epochs` — so that raising either leaves existing logs
    resumable instead of orphaning them. Steward relies on that: `observe`
    computes `samples × epochs` separately and calls a short log `SHORT`. So a
    ten-sample run relaunched for twenty is the same identifier and a genuinely
    different set of results, and a digest over identifiers alone let the first
    signature cover the second in silence.
    """
    short = SynthTask("probe", samples=4)
    workspace, _ = prepared(tmp_path, [short])
    write_log(workspace.logs, short)
    sign(workspace, items(workspace)[SIGNOFF_READY])
    assert SIGNOFF_READY not in items(workspace)

    longer = SynthTask("probe", samples=8)
    assert longer.identifier == short.identifier, "the premise of this test"
    prepared(tmp_path, [longer])
    write_log(workspace.logs, longer)

    assert SIGNOFF_READY in items(workspace)


def test_acknowledging_a_stall_settles_the_task_it_names(tmp_path: Path) -> None:
    """Two acts, one meaning: a decision that a task's results stand without it.

    An `accept` ruling latches a task; acknowledging a stall says *this will not
    be run again and the results stand without it*, which `anomalies.md` has
    been printing as a caveat in those words since the file existed. Only the
    ruling was counted, so an acknowledged stall was work outstanding forever —
    and the gate's remedy is *rule the class*, which a stall need not have: the
    guard fires on attempt history, not on an anomaly.
    """
    workspace, _ = prepared(tmp_path, [TASK, STUCK])
    write_log(workspace.logs, TASK)
    stalling(workspace, 3)
    stalled = items(workspace)[STALLED]
    assert SIGNOFF_READY not in items(workspace), "the premise: it reads unfinished"

    ack(workspace, stalled)

    after = items(workspace)
    assert SIGNOFF_READY in after
    assert STALLED not in after
    # the log really is short, and the surface goes on saying so
    assert turn(workspace).summary.states["incomplete"] == 1


def test_relaunching_the_same_manifest_un_signs_it(tmp_path: Path) -> None:
    """An unchanged task set has an unchanged digest, and a launch still restarts work.

    A launch is the one act that decides desired state — it releases the
    acceptance latches, so a task somebody accepted is queued again. Judging
    the signature on the digest alone would leave 🔒 standing over a run that
    had gone back to work, and the results the signer accepted would be
    overwritten under an attestation still claiming to cover them.
    """
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    sign(workspace, items(workspace)[SIGNOFF_READY])
    assert turn(workspace).verdict is Verdict.SIGNED_OFF

    append_event(workspace.journal, LAUNCHED, definition="evalset.py", tasks=1)

    after = turn(workspace)
    assert after.verdict is not Verdict.SIGNED_OFF
    assert SIGNOFF_READY in {item.kind for item in after.items}


# --- the id is the re-notification policy -------------------------------


def test_acknowledging_removes_an_item_from_the_turn_entirely(
    tmp_path: Path,
) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")

    ack(workspace, "unreadable:broken.eval")
    result = turn(workspace)

    # the run's one task completed, so what remains is the acceptance nobody
    # has given -- which is the point of that item and not a leak from this one
    assert [item.kind for item in result.items] == [SIGNOFF_READY]
    assert result.verdict is Verdict.COMPLETE

    # and it **moved** rather than vanishing: gone from the decisions, present
    # in what happened with who decided and why. A disposal that erased itself
    # would take *somebody dealt with this at 2am* with it, which is exactly
    # what a reader arriving at six has no other way to learn
    rendered = collect_markdown(result)
    decisions, _, history = rendered.partition("## what happened")
    assert "broken.eval" not in decisions
    assert "accepted by operator" in history and "broken.eval" in history


def test_a_second_edit_is_heard_again(tmp_path: Path) -> None:
    # the whole reason ids are keyed the way they are: accepting one deliberate
    # edit must not accept every future one, or the check is off for the run
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.root / DEFINITION).write_bytes(b"# first edit\n")

    ack(workspace, items(workspace)[DRIFT].id)
    assert DRIFT not in items(workspace)

    (workspace.root / DEFINITION).write_bytes(b"# second edit\n")

    assert DRIFT in items(workspace)


def test_acknowledging_one_bad_file_does_not_silence_the_next(
    tmp_path: Path,
) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "first.eval").write_bytes(b"not a log")

    ack(workspace, "unreadable:first.eval")
    (workspace.logs / "second.eval").write_bytes(b"also not a log")

    assert items(workspace)[UNREADABLE].id == "unreadable:second.eval"


def test_a_stall_that_gets_worse_is_a_new_item(tmp_path: Path) -> None:
    # keyed on the attempt count, so a task that fails again after somebody
    # accepted the last failure is worth saying again
    workspace, _ = prepared(tmp_path, [STUCK])
    stalling(workspace, 3)
    first = items(workspace)[STALLED]

    ack(workspace, first.id)
    assert STALLED not in items(workspace)

    # somebody intervened, it was tried once more, and it got no further
    stalling(workspace, 1, first=20)

    later = items(workspace).get(STALLED)
    assert later is not None and later.id != first.id


# --- the verdict --------------------------------------------------------

VERDICTS: list[tuple[str, list[Level], bool, int, int, int, int, Verdict]] = [
    ("nothing open", [], False, 2, 0, 1, 0, Verdict.CLEAR),
    ("paused beats everything", [Level.BLOCKING], True, 0, 0, 3, 0, Verdict.PAUSED),
    ("paused beats a clear run", [], True, 2, 0, 1, 0, Verdict.PAUSED),
    (
        "work continues around it",
        [Level.ATTENTION],
        False,
        4,
        0,
        2,
        0,
        Verdict.ATTENTION,
    ),
    ("nothing left to run it", [Level.ATTENTION], False, 0, 0, 1, 0, Verdict.STOPPED),
    ("a slot will free up", [Level.ATTENTION], False, 0, 1, 1, 0, Verdict.ATTENTION),
    (
        "finished, with a caveat",
        [Level.ATTENTION],
        False,
        0,
        0,
        0,
        0,
        Verdict.ATTENTION,
    ),
    # a park subtracts from what is effectively running. One of twenty is a run
    # working around a decision; twenty of twenty is the same arithmetic
    # reaching zero, which is what "at the ceiling they stop it" means
    ("one park of twenty", [Level.BLOCKING], False, 20, 0, 20, 1, Verdict.ATTENTION),
    ("twenty parks of twenty", [Level.BLOCKING], False, 20, 0, 20, 20, Verdict.STOPPED),
    (
        "every worker parked, but one is starting",
        [Level.BLOCKING],
        False,
        20,
        1,
        20,
        20,
        Verdict.ATTENTION,
    ),
    # ...and a fleet where everything is parked has still *finished* if there is
    # no work left, which is the same guard `unfinished` gives every other case
    (
        "parked with nothing left to do",
        [Level.ATTENTION],
        False,
        2,
        0,
        0,
        2,
        Verdict.ATTENTION,
    ),
]


@pytest.mark.parametrize(
    ("levels", "paused", "running", "spawning", "unfinished", "parked", "expected"),
    [case[1:] for case in VERDICTS],
    ids=[case[0] for case in VERDICTS],
)
def test_the_verdict_describes_the_run_rather_than_its_worst_item(
    levels: list[Level],
    paused: bool,
    running: int,
    spawning: int,
    unfinished: int,
    parked: int,
    expected: Verdict,
) -> None:
    given = [
        Item(
            id=f"k:{n}",
            kind="k",
            owner=Owner.OPERATOR,
            level=level,
            subject="",
            summary="",
        )
        for n, level in enumerate(levels)
    ]

    assert (
        verdict(
            given,
            paused=paused,
            running=running,
            spawning=spawning,
            unfinished=unfinished,
            parked=parked,
        )
        is expected
    )


def test_the_verdict_line_says_who_is_holding_it(tmp_path: Path) -> None:
    # it is a notification's title (step 24), so it stands alone: the state and
    # who has to act, and nothing about which item
    mine = Item(
        id="a",
        kind="k",
        owner=Owner.OPERATOR,
        level=Level.ATTENTION,
        subject="",
        summary="",
    )
    theirs = Item(
        id="b",
        kind="k",
        owner=Owner.AGENT,
        level=Level.ATTENTION,
        subject="",
        summary="",
    )

    assert verdict_line(Verdict.CLEAR, []) == "✅ nothing needs you"
    assert verdict_line(Verdict.ATTENTION, [mine]) == "⚠️ 1 needs an operator"
    assert (
        verdict_line(Verdict.ATTENTION, [mine, theirs])
        == "⚠️ 1 needs an operator, 1 for the agent"
    )
    assert verdict_line(Verdict.PAUSED, [mine]).startswith("⏸ paused")
    assert "nothing is progressing" in verdict_line(Verdict.STOPPED, [mine])


# --- the diff -----------------------------------------------------------


def test_a_turn_reports_what_changed_since_the_last_one(tmp_path: Path) -> None:
    # what step 24 fires on, and why it is set arithmetic rather than counts:
    # one item resolved and another arriving in the same turn must not read as
    # no change
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "first.eval").write_bytes(b"not a log")

    first = turn(workspace)
    # `in` rather than `==`: this fixture's one task is complete, so the first
    # turn also raises the acceptance item. The claim under test is the pair of
    # assertions below, where that item persists and therefore appears in neither
    assert "unreadable:first.eval" in first.appeared
    assert first.resolved == []

    (workspace.logs / "first.eval").unlink()
    (workspace.logs / "second.eval").write_bytes(b"not a log")

    second = turn(workspace)
    assert second.appeared == ["unreadable:second.eval"]
    assert second.resolved == ["unreadable:first.eval"]


def test_a_condition_that_persists_is_not_reported_as_new(tmp_path: Path) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")

    turn(workspace)
    second = turn(workspace)

    assert second.appeared == [] and second.resolved == []
    assert [item.id for item in second.items if item.kind == UNREADABLE] == [
        "unreadable:broken.eval"
    ]


def test_a_status_diffs_against_the_last_tend_without_recording_one(
    tmp_path: Path,
) -> None:
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    turn(workspace)
    (workspace.logs / "broken.eval").write_bytes(b"not a log")

    before = len(workspace.journal.read_text().splitlines())
    previewed = status(workspace)

    assert previewed.appeared == ["unreadable:broken.eval"]
    assert len(workspace.journal.read_text().splitlines()) == before
    # and a status twice running says the same thing, since nothing advanced
    assert status(workspace).appeared == previewed.appeared


# --- supervision ---------------------------------------------------------
#
# The item a timer creates the need for, and the one condition in this file that
# is about Steward's own machinery rather than about the run. Its whole design
# problem is staying quiet: a workspace somebody is driving by hand has not lost
# supervision, so the item is about an *expectation that broke* and there has to
# have been one.


def armed(
    workspace: Workspace,
    *,
    interval: int = 600,
    scheduler: str = "cron",
    ago: int = 0,
) -> None:
    """Record a timer, optionally as of `ago` seconds in the past.

    A silence has to be longer than the timer that failed to fill it, so any case
    about staleness needs an arming that predates the gap — `ago` is how a
    clock-free projection is told that. The payload's `ts` overrides the envelope's
    because `append_event` merges the fields last.
    """
    stamp = datetime.now(timezone.utc) - timedelta(seconds=ago)
    append_event(
        workspace.journal,
        ARMED,
        ts=stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        scheduler=scheduler,
        interval=interval,
        label="steward-aaa",
    )


def aged(workspace: Workspace, seconds: int) -> None:
    """Backdate every recorded turn, as if the timer had stopped firing.

    The only thing a clock-free projection can be given: the item compares the
    last turn's timestamp against now, so making a run look unsupervised means
    moving the timestamps rather than the clock. Goes with `armed(ago=...)` — a
    gap only counts against a timer that was there for it.
    """
    moved = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    lines = workspace.journal.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        event: dict[str, Any] = json.loads(line)
        if event["type"] == OBSERVATION:
            event["ts"] = moved.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
        rewritten.append(json.dumps(event))
    workspace.journal.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def unfinished(tmp_path: Path) -> Workspace:
    """A run with work left, which is the only kind supervision matters for."""
    workspace, _ = prepared(tmp_path, [TASK, SynthTask("waiting")])
    write_log(workspace.logs, TASK)
    return workspace


def test_a_run_nobody_ever_armed_is_not_reported_as_unsupervised(
    tmp_path: Path,
) -> None:
    # a workspace somebody is driving by hand at their terminal, and telling
    # them so every ten minutes is how an attention list stops being read
    workspace = unfinished(tmp_path)

    assert UNSUPERVISED not in items(workspace)


def test_a_timer_that_was_armed_and_is_gone_is_reported(tmp_path: Path) -> None:
    workspace = unfinished(tmp_path)
    armed(workspace)
    append_event(workspace.journal, DISARMED, scheduler="cron")

    item = items(workspace)[UNSUPERVISED]

    assert item.owner is Owner.OPERATOR
    assert item.action == "steward timer arm"
    assert item.acknowledgeable
    # acknowledging says *I am driving this by hand*, which stays true -- so no
    # discriminator, and disarming again is the same statement not a new one
    assert item.id == UNSUPERVISED


def test_a_timer_tending_on_time_says_nothing(tmp_path: Path) -> None:
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600)
    turn(workspace)

    assert UNSUPERVISED not in {item.kind for item in status(workspace).items}


def test_a_timer_that_has_stopped_firing_is_reported(tmp_path: Path) -> None:
    # the better signal of the two, because it catches a crontab somebody
    # edited by hand as well as one Steward removed
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)
    turn(workspace)
    aged(workspace, 3600)

    (item,) = [entry for entry in status(workspace).items if entry.kind == UNSUPERVISED]

    assert "has not tended" in item.summary
    assert "10m" in item.summary
    assert item.subject == "cron"


def test_a_recovery_tend_does_not_report_the_silence_it_just_ended(
    tmp_path: Path,
) -> None:
    """The staleness half is vacuous during a tend and meaningful during a `status`.

    The gap is measured against the previous turn, so a tend that has just
    converged the run would otherwise announce the very silence it broke — and
    the reader who needs telling that supervision stopped is the operator typing
    `status`, not the timer that is evidently working.
    """
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)
    turn(workspace)
    aged(workspace, 3600)
    assert UNSUPERVISED in {entry.kind for entry in status(workspace).items}

    assert UNSUPERVISED not in {entry.kind for entry in turn(workspace).items}


def test_re_arming_resets_the_clock_the_staleness_check_reads(tmp_path: Path) -> None:
    """A timer armed a moment ago has not been silent for the hours before it existed.

    Otherwise `steward timer arm` — the remedy the item's own `action` names —
    appears not to have worked: the operator arms a replacement and `status`
    immediately tells them the new timer has been quiet since last night.
    """
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)
    turn(workspace)
    aged(workspace, 3600)
    assert UNSUPERVISED in {entry.kind for entry in status(workspace).items}

    armed(workspace, interval=600)

    assert UNSUPERVISED not in {entry.kind for entry in status(workspace).items}


def test_a_timer_armed_and_never_tended_at_all_is_reported(tmp_path: Path) -> None:
    # the other direction the same comparison closes: with nothing but the last
    # tend to go on, a run that has never had one reads as *no evidence of a
    # problem* when it is the plainest case of one
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)

    (item,) = [entry for entry in status(workspace).items if entry.kind == UNSUPERVISED]

    assert "has not tended" in item.summary


def test_a_stale_timer_re_armed_asks_the_question_again(tmp_path: Path) -> None:
    # keyed on when this timer was armed, so an acknowledged silence does not
    # cover the next timer's
    workspace = unfinished(tmp_path)
    armed(workspace, interval=600, ago=7200)
    turn(workspace)
    aged(workspace, 3600)
    first = [e for e in status(workspace).items if e.kind == UNSUPERVISED][0]

    ack(workspace, first.id)
    # an hour ago rather than now, because the replacement has to have had a
    # chance to fire and missed it -- re-arming this instant is quiet, which is
    # the case above
    armed(workspace, interval=600, ago=3600)

    second = [e for e in status(workspace).items if e.kind == UNSUPERVISED]
    assert second and second[0].id != first.id


def test_a_finished_run_needs_no_timer(tmp_path: Path) -> None:
    # signing a run off disarms its timer deliberately, and reporting that as
    # lost supervision would make the last act of every run produce an item
    workspace, _ = prepared(tmp_path, [TASK])
    write_log(workspace.logs, TASK)
    armed(workspace)
    append_event(workspace.journal, DISARMED, scheduler="cron")

    assert UNSUPERVISED not in items(workspace)


def test_an_interval_the_workspace_no_longer_asks_for_is_reported(
    tmp_path: Path,
) -> None:
    workspace = unfinished(tmp_path)
    armed(workspace, interval=1800)
    workspace.directives.write_text("tend_interval: 5m\n", encoding="utf-8")

    item = items(workspace)[TIMER_DRIFT]

    assert "every 30m" in item.summary and "asks for 5m" in item.summary
    assert item.action == "steward timer arm"


def test_editing_the_interval_again_asks_the_question_again(tmp_path: Path) -> None:
    # both values key the id, so a further edit is a new question and arming to
    # match clears it
    workspace = unfinished(tmp_path)
    armed(workspace, interval=1800)
    workspace.directives.write_text("tend_interval: 5m\n", encoding="utf-8")
    first = items(workspace)[TIMER_DRIFT]

    ack(workspace, first.id)
    workspace.directives.write_text("tend_interval: 1m\n", encoding="utf-8")

    assert items(workspace)[TIMER_DRIFT].id != first.id


def test_a_timer_armed_at_the_interval_asked_for_is_quiet(tmp_path: Path) -> None:
    workspace = unfinished(tmp_path)
    armed(workspace, interval=1800)
    workspace.directives.write_text("tend_interval: 30m\n", encoding="utf-8")

    assert TIMER_DRIFT not in items(workspace)


def test_a_broken_steward_md_does_not_also_report_timer_drift(
    tmp_path: Path,
) -> None:
    # one complaint per broken file: the degraded item already says the file
    # could not be read, and a second saying its interval disagrees would be
    # reporting a value nobody could have written
    workspace = unfinished(tmp_path)
    armed(workspace, interval=1800)
    turn(workspace)
    workspace.directives.write_text("not: [valid\n", encoding="utf-8")

    found = items(workspace)
    assert DEGRADED in found
    assert TIMER_DRIFT not in found


def test_a_one_off_interval_against_a_silent_file_is_not_drift(
    tmp_path: Path,
) -> None:
    """The comparison is against what the workspace *expressed*, not what resolved.

    An operator who armed `--interval 1m` against a `_steward.yaml` with no
    opinion about intervals has not created a conflict — and reporting one
    would be reporting drift from Steward's own default, a number nobody wrote.
    Found by arming a real timer and being told, one second later, that the
    workspace wanted something else.
    """
    workspace = unfinished(tmp_path)
    armed(workspace, interval=60)

    assert TIMER_DRIFT not in items(workspace)


# --- the write-up, and when it is owed -----------------------------------


class TestWhenAWriteUpIsAskedFor:
    """`unwritten` waits for results, because it is a question about results.

    The section itself appears with a task's first log, which is right: the
    facts are worth keeping current from the moment there are any. The *item*
    used to appear then too, and that is a different claim — it asks somebody
    to explain numbers that are still moving. A four-task run raised four of
    them while every task was mid-flight, which is the same way an attention
    list stops being read that `analysis_sections` already guards against one
    step earlier.
    """

    def collected(self, workspace: Workspace) -> None:
        """The first collect is what creates the obligation at all."""
        append_event(workspace.journal, COLLECTED, position=0)

    def test_a_task_still_running_is_not_asked_to_explain_itself(
        self, tmp_path: Path
    ) -> None:
        workspace, _ = prepared(tmp_path, [STUCK])
        # four of the ten the manifest asks for: a log exists, so the section
        # exists, and the task is plainly not done with it
        write_log(workspace.logs, STUCK, total=4, completed=4)
        self.collected(workspace)

        assert UNWRITTEN not in items(workspace)

    def test_and_is_once_it_has_finished(self, tmp_path: Path) -> None:
        workspace, _ = prepared(tmp_path, [TASK])
        write_log(workspace.logs, TASK)
        self.collected(workspace)

        found = items(workspace)

        assert UNWRITTEN in found
        assert found[UNWRITTEN].owner is Owner.AGENT

    def test_a_run_nobody_attached_to_is_owed_nothing(self, tmp_path: Path) -> None:
        # no collect, so no agent ever picked this up and there is nobody the
        # item is addressed to
        workspace, _ = prepared(tmp_path, [TASK])
        write_log(workspace.logs, TASK)

        assert UNWRITTEN not in items(workspace)

    def test_several_owed_write_ups_are_one_item(self, tmp_path: Path) -> None:
        """Four near-identical lines for what a reader sees as one job.

        Nothing was bought by the granularity — `UNWRITTEN` cannot be
        acknowledged, so the per-task ids could not be disposed of one at a
        time and existed only to be printed, crowding out whatever else the
        queue was trying to say.
        """
        other = SynthTask("second", samples=4)
        workspace, _ = prepared(tmp_path, [TASK, other])
        write_log(workspace.logs, TASK)
        write_log(workspace.logs, other)
        self.collected(workspace)

        raised = [item for item in turn(workspace).items if item.kind == UNWRITTEN]

        assert len(raised) == 1
        assert "2 tasks have no write-up" in raised[0].summary
        # and it still says which, because the count alone is not actionable
        assert "probe" in raised[0].summary
        assert "second" in raised[0].summary
