"""Which running workers have an operator channel to reach.

A detached worker binds an ACP server as well as its control channel — Steward's control-channel reads are `live.py`'s business, and this is the other socket. It is how an operator answers an approval or an `ask_user` question in a process with no terminal, and Steward's part in that is to know **whether there is anything to reach**: a pid with a socket in the discovery directory can be attached to, and a pid without one cannot.

**No ACP client of its own, deliberately.** Answering an approval is authority over what an eval measures, and that belongs to the operator (agent.md §6) — so Steward detects the wait and inspect's own TUI does the talking. The consequence worth noticing is that attaching is not special to a parked worker: any live worker can be reached this way, which is the standing answer to *a detached run cannot be watched*.

**And the address is not passed on, only its existence.** The command Steward names is the bare `inspect acp`, because that is the one that works: its picker lists every eval discovery can find and floats the samples waiting on an operator to the top, which is precisely the sort a reader of a park needs. `--server` bypasses discovery, which upstream documents as the way to reach a *remote* machine or to override what was found — and it takes a path that is per-pid, so a respawn between the item being raised and somebody reading it leaves a command that connects to nothing.
"""

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
