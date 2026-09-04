"""Whether a worker's pid has an operator channel bound to it.

Against the real discovery directory — inspect's own writer, inspect's own
reader, and the pid of the process running the test, which is the one pid that
is certainly alive. What is not here is an ACP conversation: Steward has no
client and never answers, so the whole of its part is knowing that somebody
else could.
"""

import os
from pathlib import Path

from inspect_steward._worker import acp_sockets

from .._acp import Publish, publish

__all__ = ["publish"]


def test_a_worker_s_socket_is_found_by_its_pid(
    tmp_path: Path, publish: Publish
) -> None:
    # a pid is what an external runner knows about a process it spawned, and
    # until `DiscoveredEval` carried one there was no way from that to this
    socket = tmp_path / "w.sock"
    publish(os.getpid(), socket)

    assert acp_sockets()[os.getpid()] == socket


def test_a_worker_with_no_acp_server_is_simply_absent(
    tmp_path: Path, publish: Publish
) -> None:
    # an ordinary answer rather than a fault: the bind degrades when a path
    # cannot be bound, and an older inspect never had one. It costs the attach
    # command, not the report that somebody is waiting
    publish(os.getpid(), tmp_path / "w.sock")

    assert acp_sockets().get(os.getpid() + 1_000_000) is None


def test_a_dead_worker_s_socket_is_not_offered(
    tmp_path: Path, publish: Publish
) -> None:
    # the address would be unreachable, and printing one is worse than printing
    # none: it sends an operator to a socket nothing is listening on
    dead = 2**22 - 1
    publish(dead, tmp_path / "gone.sock")

    assert dead not in acp_sockets()
