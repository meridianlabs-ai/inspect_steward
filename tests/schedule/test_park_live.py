"""A detached worker waiting for a person, on a real worker.

The one claim in step 20 no synthesized state can make, and the reason the step
exists at all. Human input dispatches **ACP → Textual panel → console**; a
Steward worker has no display and a closed stdin, so before this the last of the
three raised `EOFError` into the tool call and a request for a human decision
landed as an errored sample in an otherwise successful log — not a hang, not a
visible failure, an anomaly that did not say what it was.

Everything on the way to the assertion is on the other side of a process
boundary: `eval_set` turning the ACP server on for a selection-mode worker, the
routing shim committing to it, the control channel classifying the wait ahead of
the tool call it is gating, and the discovery file that gets a pid back to a
socket. Each of those is unit-tested against a fixture of the next one's shape,
and a fixture is a belief about that shape.

**Budget: one launch**, held open by the eval doing what it was asked — no fault
marker, no sleep. The sample stops at the approval and stays there, which is
exactly the condition under test.

What is asserted about the attach command is that the address is *live*: the
socket exists and accepts a connection. Driving an ACP conversation over it
would be testing inspect's client, which is the piece Steward deliberately does
not have.
"""

import socket as socketlib
from pathlib import Path

import pytest
from inspect_ai.agent._acp.discovery import list_discovered_evals
from inspect_steward._launch import Launch, launch
from inspect_steward._schedule import RunningWorker
from inspect_steward._tend import TendResult, status
from inspect_steward._tend.items import PARKED, Item, Level, Owner
from inspect_steward._worker import resolve_inflight
from inspect_steward._workspace import Workspace, create_workspace

from .._fault import until
from ..timer._fake import clear_credentials, fake_cron

FIXTURES = Path(__file__).parents[1] / "evalset" / "fixtures"
DEFINITION = "approval_evalset.py"


def parked(result: TendResult) -> Item | None:
    return next((item for item in result.items if item.kind == PARKED), None)


def test_a_worker_waiting_on_an_approval_is_reported_with_the_way_to_answer_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cron(monkeypatch)
    clear_credentials(monkeypatch)

    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    definition = workspace.root / DEFINITION
    definition.write_bytes((FIXTURES / DEFINITION).read_bytes())

    started = launch(workspace, definition)
    assert isinstance(started, Launch), f"refused by {started}"
    assert started.turn is not None and len(started.turn.spawned) == 1

    # a state to wait for rather than a delay to outlast: the sample reaches
    # the approval when it reaches it, and then stays there
    found: list[Item] = []

    def waiting() -> bool:
        item = parked(status(workspace))
        if item is not None:
            found.append(item)
        return bool(found)

    until("the worker to park on an approval", waiting)
    item = found[0]

    # the whole item, and each part of it for its own reason
    assert item.owner is Owner.HUMAN
    assert item.level is Level.BLOCKING
    assert not item.acknowledgeable
    # the tool the model asked to call, which is the one structural part of a
    # request; the arguments are the model's own words
    assert "is waiting on an approval for echo" in item.summary

    # the bare verb, offered because the pid Steward spawned really does have
    # a socket something is listening on -- the address is what proves there is
    # anything to reach, not what the reader is asked to paste
    assert item.action == "inspect acp"
    published = {
        entry.pid: entry.target.socket_path for entry in list_discovered_evals()
    }
    ((pid, path),) = published.items()
    assert path is not None and path.exists()
    with socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))

    # nothing reaps or replaces it while it waits — the slot stays held, which
    # is what makes enough parked workers stall a fleet
    settled = status(workspace)
    assert settled.summary.running == 1
    assert settled.spawned == [] and settled.reaped == []
    assert pid in {worker.pid for worker in _running(workspace)}


def _running(workspace: Workspace) -> list[RunningWorker]:
    return list(resolve_inflight(workspace.inflight, workspace.workers).running)
