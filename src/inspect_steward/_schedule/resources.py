"""How many cores this process actually has.

The worker pool's ceiling is core count (scheduling.md, *Launch everything, up to a ceiling*), and `os.cpu_count()` is the wrong number in exactly the deployment where being wrong is fatal: a Kubernetes pod limited to 2 CPUs on a 64-core node reports 64, so a cores-derived ceiling over-spawns by 32× and takes the pod down.

Three limits can each be the binding one, so the answer is the smallest of them:

- the **cgroup CPU quota**, which is what a container's `--cpus` or a pod's `limits.cpu` sets
- the **affinity mask**, which is what a cpuset or `taskset` sets
- `os.cpu_count()`, the machine

This lives beside `reconcile` rather than in it: reading `/sys` is I/O, and `reconcile` is pure. The ceiling is an argument to the decision, not part of it.
"""

import math
import os
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")

_CGROUP_V2 = "cpu.max"
_CGROUP_V1_QUOTA = "cpu/cpu.cfs_quota_us"
_CGROUP_V1_PERIOD = "cpu/cpu.cfs_period_us"


def available_cores(cgroup_root: Path = CGROUP_ROOT) -> int:
    """Cores this process can actually use.

    Args:
        cgroup_root: Where to look for cgroup limits (overridable for testing).

    Returns:
        The smallest of the cgroup CPU quota, the affinity mask, and the machine's processor count. At least 1 — a quota below one core still permits one worker, which will simply run slowly.
    """
    limits = [
        limit
        for limit in (
            os.cpu_count(),
            _affinity_cores(),
            cores_from_cgroup(cgroup_root),
        )
        if limit is not None
    ]
    return max(1, min(limits)) if limits else 1


def _affinity_cores() -> int | None:
    """Cores in this process's affinity mask (`None` where the platform has no such notion)."""
    getaffinity = getattr(os, "sched_getaffinity", None)
    return len(getaffinity(0)) if getaffinity is not None else None


def cores_from_cgroup(root: Path) -> int | None:
    """Whole cores permitted by the cgroup CPU quota.

    Handles both hierarchies, because which one a host uses is not something Steward gets to choose: v2 writes `"<quota> <period>"` (or `"max <period>"`) to `cpu.max`, v1 splits the pair across `cpu.cfs_quota_us` and `cpu.cfs_period_us` and writes `-1` for unlimited.

    Args:
        root: cgroup filesystem root.

    Returns:
        Whole cores, rounded up so a fractional quota still yields one; `None` when there is no cgroup, no quota, or the files are unreadable — all of which mean *unlimited* rather than *zero*.
    """
    v2 = _read_int_pair(root / _CGROUP_V2)
    if v2 is not None:
        quota, period = v2
        return _cores(quota, period)

    quota_only = _read_int(root / _CGROUP_V1_QUOTA)
    period_only = _read_int(root / _CGROUP_V1_PERIOD)
    if quota_only is not None and period_only is not None:
        return _cores(quota_only, period_only)

    return None


def _cores(quota: int | None, period: int | None) -> int | None:
    """Whole cores from a quota/period pair, rounding up."""
    if quota is None or quota <= 0 or period is None or period <= 0:
        return None
    return max(1, math.ceil(quota / period))


def _read_int_pair(path: Path) -> tuple[int | None, int] | None:
    """Read cgroup v2 `cpu.max` as `(quota, period)`, with `None` quota for `"max"`."""
    text = _read_text(path)
    if text is None:
        return None
    fields = text.split()
    if len(fields) != 2:
        return None
    quota = None if fields[0] == "max" else _parse_int(fields[0])
    period = _parse_int(fields[1])
    return (quota, period) if period is not None else None


def _read_int(path: Path) -> int | None:
    text = _read_text(path)
    return _parse_int(text) if text is not None else None


def _read_text(path: Path) -> str | None:
    # a cgroup file can be absent, or present and unreadable in a sandbox; both
    # mean "no limit stated here" rather than an error worth propagating
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _parse_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None
