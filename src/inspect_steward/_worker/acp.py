"""The command that reaches a running worker's human channel.

A detached worker binds an ACP server as well as its control channel — Steward's control-channel reads are `live.py`'s business, and this is the other socket. It is how a person answers an approval or an `ask_user` question in a process with no terminal, and Steward's whole part in that is **naming the command**: resolve the worker's socket from the discovery directory and print `inspect acp --server <socket>`.

**No ACP client of its own, deliberately.** Answering an approval is authority over what an eval measures, and that belongs to the human (agent.md §6) — so Steward detects the wait and hands over the address, and inspect's own TUI does the talking. The consequence worth noticing is that the command is not special to a parked worker: any live worker can be attached to this way, which is the standing answer to *a detached run cannot be watched*.

`--server` is used rather than `--eval-id` because a pid is what Steward knows about a worker it spawned, and the flag bypasses discovery entirely — so the command keeps working from a shell whose `inspect` would otherwise have to pick among a machine's worth of running evals.
"""

import shlex
from pathlib import Path

from inspect_ai.agent._acp.discovery import list_discovered_evals


def acp_sockets() -> dict[int, Path]:
    """Every live ACP server on this machine, by the pid that bound it.

    The whole directory in one read, mirroring how `resolve_inflight` picks up control sockets — a caller with several parked workers asks once rather than once each.

    A pid **missing** from the mapping is an ordinary answer rather than a fault: a worker running an older inspect never bound one, and a worker still starting up has not bound one yet. What it costs is the attach command, so a caller reports the wait without an address rather than not reporting the wait.

    Returns:
        Socket path per pid, over the servers whose process is still alive.
    """
    return {
        entry.pid: entry.target.socket_path
        for entry in list_discovered_evals()
        if entry.pid and entry.target.socket_path is not None
    }


def attach_command(socket: Path) -> str:
    """The shell command that attaches a person to a worker's ACP server.

    Quoted where it has to be, because the default path is one macOS produces with a space in it — `~/Library/Application Support/inspect_ai/acp/<pid>.sock`. A command printed for somebody to paste is wrong if pasting it does not work.
    """
    return f"inspect acp --server {shlex.quote(str(socket))}"
