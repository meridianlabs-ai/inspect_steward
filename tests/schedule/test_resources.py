"""Core count, which is not `os.cpu_count()` where it matters most.

A pod limited to 2 CPUs on a 64-core node reports 64. Every row here is a
cgroup layout a real host produces, written into `tmp_path` so the test runs
on a machine that has no cgroups at all.
"""

import os
from pathlib import Path

import pytest
from inspect_steward._schedule import available_cores, cores_from_cgroup


def write(root: Path, name: str, contents: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param({"cpu.max": "200000 100000"}, 2, id="v2_two_cores"),
        pytest.param({"cpu.max": "max 100000"}, None, id="v2_unlimited"),
        # a fractional quota still permits a worker; it just runs slowly
        pytest.param({"cpu.max": "50000 100000"}, 1, id="v2_half_a_core"),
        pytest.param({"cpu.max": "250000 100000"}, 3, id="v2_rounds_up"),
        pytest.param(
            {"cpu/cpu.cfs_quota_us": "400000", "cpu/cpu.cfs_period_us": "100000"},
            4,
            id="v1_four_cores",
        ),
        pytest.param(
            {"cpu/cpu.cfs_quota_us": "-1", "cpu/cpu.cfs_period_us": "100000"},
            None,
            id="v1_unlimited",
        ),
        pytest.param({}, None, id="no_cgroup"),
        # a file that exists but says something unexpected means "no limit
        # stated here", not zero cores
        pytest.param({"cpu.max": "garbage"}, None, id="unparseable"),
    ],
)
def test_cores_from_cgroup(
    files: dict[str, str], expected: int | None, tmp_path: Path
) -> None:
    for name, contents in files.items():
        write(tmp_path, name, contents)

    assert cores_from_cgroup(tmp_path) == expected


def test_v2_wins_over_v1(tmp_path: Path) -> None:
    # a host running both hierarchies is the v2 one
    write(tmp_path, "cpu.max", "200000 100000")
    write(tmp_path, "cpu/cpu.cfs_quota_us", "1600000")
    write(tmp_path, "cpu/cpu.cfs_period_us", "100000")

    assert cores_from_cgroup(tmp_path) == 2


def test_the_smallest_limit_binds(tmp_path: Path) -> None:
    # the whole point: a one-core quota beats whatever the machine reports
    write(tmp_path, "cpu.max", "100000 100000")

    assert available_cores(tmp_path) == 1


def test_without_a_cgroup_the_machine_is_the_limit(tmp_path: Path) -> None:
    machine = os.cpu_count() or 1
    affinity = getattr(os, "sched_getaffinity", None)
    expected = min(machine, len(affinity(0))) if affinity is not None else machine

    assert available_cores(tmp_path) == expected


def test_never_zero(tmp_path: Path) -> None:
    # a quota of a hundredth of a core still permits one worker
    write(tmp_path, "cpu.max", "1000 100000")

    assert available_cores(tmp_path) == 1
