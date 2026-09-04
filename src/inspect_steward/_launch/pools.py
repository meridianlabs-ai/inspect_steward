"""Docker's default address pools, which cap sandbox concurrency far below the host.

Every sandboxed sample is its own compose project — `DockerSandboxEnvironment.sample_init` calls `ComposeProject.create` per sample, not per task — and every compose project gets its own bridge network. Docker allocates those out of `default-address-pools`, whose built-in value is two ranges carved coarsely: `172.17.0.0/12` at `/16` and `192.168.0.0/16` at `/20`, sixteen networks each. **Thirty-two between them**, less the default `bridge` and whatever else is up, so a host allocates about thirty.

That is a ceiling on *concurrently running samples across the whole machine*, and nothing about it scales with the machine. A 64-core host whose provider offers 128 sandboxes runs thirty of them and fails every sample after with

    could not find an available, non-overlapping IPv4 address pool among the defaults to assign to the network

which reads like a network fault rather than a ceiling somebody is allowed to raise. Carving the same two ranges finer — `/20` and `/24` — yields 512 networks of 4096 and 256 addresses, which is past anything one host will run, out of the same RFC1918 space the defaults already claim.

**The daemon reports this by omission**, which is what makes detecting it one subprocess rather than an argument about config files. `docker info` carries `DefaultAddressPools` only where it was configured, so an absent field *is* the built-in default rather than an unknown. Steward reads the daemon and never infers from `daemon.json`, which says what was asked for rather than what is in force — the file is consulted only when writing a fix into it.

**Steward writes the file and never restarts the daemon.** A restart is what makes the change take effect and it kills every running container on the host — including work Steward has no claim on, which on a shared eval box belongs to somebody else. Writing is one `mv` from undone and needs no privilege on Docker Desktop, where the file is the user's own; the restart is left to the operator, with the command for their platform printed.

See https://straz.to/2021-09-08-docker-address-pools/, which is where this ceiling is worked out.
"""

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_network
from pathlib import Path
from typing import Any, cast

DEFAULT_POOLS: tuple[tuple[str, int], ...] = (
    ("172.17.0.0/12", 16),
    ("192.168.0.0/16", 20),
)
"""What a daemon that was never configured allocates from. Sixteen networks each."""

RECOMMENDED_POOLS: tuple[tuple[str, int], ...] = (
    ("172.17.0.0/12", 20),
    ("192.168.0.0/16", 24),
)
"""The same two ranges carved finer: 256 networks each, of 4096 and 256 addresses.

Deliberately not a new range. Widening into `10.0.0.0/8` would buy 65,536 networks and a much better chance of colliding with something the host reaches over a VPN — these two are already Docker's, so the blast radius of the change is confined to how finely space Docker had anyway is divided.
"""

PROBE_TIMEOUT = 10.0
"""Seconds to wait for `docker info`. A daemon slower than this is one whose answer a launch should not be blocking on."""

POOLS_ADVISED = "docker_pools_advised"
"""Journal `action`: this workspace has been told its Docker cannot allocate enough networks.

Recorded so it is said once. The condition belongs to the host rather than to the run, so somebody who has heard it and left their daemon alone has answered for every later launch in this workspace too.
"""

POOLS_WRITTEN = "docker_pools_written"
"""Journal `action`: the pools were written into `daemon.json`, and by whose consent.

Worth a line of its own because it is the one thing a launch does *outside* the workspace: a reader working out why this machine's Docker changed on a Tuesday should find it in the run that changed it.
"""


@dataclass(frozen=True)
class AddressPools:
    """What the local daemon allocates bridge networks from."""

    pools: tuple[tuple[str, int], ...]
    """Each pool as `(base, size)`, exactly as Docker states it."""

    configured: bool
    """Whether the daemon was told these, as against falling back to the built-in default."""

    @property
    def networks(self) -> int:
        """How many bridge networks the pools can allocate between them.

        A pool of base `/p` carved at `/s` yields `2 ** (s - p)`. A pool that will not parse contributes nothing rather than raising: this number exists to be compared against a concurrency, and a partial count that is too low advises where a crash would not.
        """
        total = 0
        for base, size in self.pools:
            try:
                prefix = ip_network(base, strict=False).prefixlen
            except ValueError:
                continue
            if size >= prefix:
                total += 2 ** (size - prefix)
        return total


@dataclass(frozen=True)
class PoolAdvice:
    """A host whose Docker will run fewer sandboxes than the run will ask for."""

    networks: int
    """Bridge networks the daemon can allocate, which is the real sandbox ceiling."""

    wanted: int
    """Concurrent sandboxes this run would use if nothing capped it."""

    config: Path
    """The `daemon.json` a fix would be written to."""

    proposed: tuple[tuple[str, int], ...] = RECOMMENDED_POOLS
    """The pools to write."""

    @property
    def proposed_networks(self) -> int:
        return AddressPools(self.proposed, configured=True).networks


def read_pools(timeout: float = PROBE_TIMEOUT) -> AddressPools | None:
    """What the local Docker daemon allocates networks from.

    Args:
        timeout: Seconds to wait for the daemon.

    Returns:
        The pools, or `None` where there is no Docker to ask — not installed, not running, or too slow. Absence is never advice: a machine with no Docker has no ceiling to raise.
    """
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "{{json .DefaultAddressPools}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None

    try:
        reported = json.loads(probe.stdout.strip() or "null")
    except ValueError:
        return None
    pools = _parse(reported)
    # the field is omitted where it was never configured, which is the whole
    # detection: an absent answer is the built-in default rather than a silence
    return AddressPools(pools or DEFAULT_POOLS, configured=bool(pools))


def advise(wanted: int, timeout: float = PROBE_TIMEOUT) -> PoolAdvice | None:
    """Whether this host's Docker will run out of networks before it runs out of room.

    Args:
        wanted: Concurrent sandboxes the run would use — the provider's own default, or a declared `max_sandboxes`.
        timeout: Seconds to wait for the daemon.

    Returns:
        The advice, or `None` where there is no Docker, or where its pools already afford what the run wants. **The comparison is the trigger, not the mere absence of configuration**: the built-in thirty is ample on a four-core laptop and the ceiling never binds there, so saying so would be noise on the machines that do not have the problem.
    """
    pools = read_pools(timeout)
    if pools is None or wanted <= pools.networks:
        return None
    return PoolAdvice(
        networks=pools.networks, wanted=wanted, config=daemon_config_path()
    )


def daemon_config_path() -> Path:
    """Where this platform's Docker daemon reads its configuration.

    Docker Desktop — macOS and Windows — keeps it in the user's own home, which is the file its *Settings → Docker Engine* pane edits, and which needs no privilege to write. A Linux daemon reads the system file, which does.
    """
    if sys.platform in ("darwin", "win32"):
        return Path.home() / ".docker" / "daemon.json"
    return Path("/etc/docker/daemon.json")


def write_pools(advice: PoolAdvice) -> Path | None:
    """Merge the proposed pools into `daemon.json`, keeping a copy of what was there.

    **A merge rather than a write**, because the file is rarely only ours: Docker Desktop ships one carrying `builder` and `experimental`, and replacing it would silently drop settings the operator chose. Only `default-address-pools` is set.

    Args:
        advice: What to write, and where.

    Returns:
        The backup written, or `None` where there was no file to back up.

    Raises:
        OSError: The file could not be read or written — on Linux, most often because `/etc/docker/daemon.json` needs root, which is a refusal to report rather than a privilege to acquire.
        ValueError: The existing file is not JSON, which is a file to show an operator rather than one to overwrite.
    """
    config: dict[str, Any] = {}
    backup: Path | None = None
    if advice.config.exists():
        text = advice.config.read_text(encoding="utf-8")
        loaded: object = json.loads(text) if text.strip() else {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{advice.config} does not hold a JSON object")
        config = cast(dict[str, Any], loaded)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = advice.config.with_name(f"{advice.config.name}.backup-{stamp}")
        shutil.copy2(advice.config, backup)

    config["default-address-pools"] = [
        {"base": base, "size": size} for base, size in advice.proposed
    ]
    advice.config.parent.mkdir(parents=True, exist_ok=True)
    advice.config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return backup


def restart_command() -> str:
    """How this platform makes a written `daemon.json` take effect."""
    if sys.platform == "darwin":
        return "osascript -e 'quit app \"Docker\"' && open -a Docker"
    if sys.platform == "win32":
        return "restart Docker Desktop"
    return "sudo systemctl restart docker"


def _parse(reported: object) -> tuple[tuple[str, int], ...]:
    """Pools out of `docker info`'s JSON, which capitalizes what `daemon.json` does not.

    An entry that will not read is skipped rather than raising. This runs to decide whether to offer advice, and a daemon reporting a shape this version does not know is a reason to stay quiet, never a reason to fail a launch.
    """
    if not isinstance(reported, list):
        return ()
    pools: list[tuple[str, int]] = []
    for entry in cast(list[object], reported):
        if not isinstance(entry, dict):
            continue
        item = cast(dict[str, Any], entry)
        base = item.get("Base", item.get("base"))
        size = item.get("Size", item.get("size"))
        if (
            isinstance(base, str)
            and isinstance(size, int)
            and not isinstance(size, bool)
        ):
            pools.append((base, size))
    return tuple(pools)
