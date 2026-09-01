"""Docker's address pools, and the one edit Steward proposes outside the workspace.

The arithmetic is the whole finding — thirty networks is a ceiling on concurrent *samples*, because every sandboxed sample is its own compose project — so it is driven as a table rather than inferred from a live daemon. The probe is exercised against synthesized `docker info` output for the same reason the cgroup readers are: the shapes that matter (an omitted field, a configured pool, junk) are ones a developer's own machine will only ever show one of.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from inspect_steward._launch.pools import (
    DEFAULT_POOLS,
    RECOMMENDED_POOLS,
    AddressPools,
    PoolAdvice,
    advise,
    daemon_config_path,
    read_pools,
    restart_command,
    write_pools,
)

CAPACITY = [
    # the built-in default: sixteen networks from each pool, and the ceiling
    # this whole module exists to raise
    ("the built-in default", DEFAULT_POOLS, 32),
    ("the recommendation", RECOMMENDED_POOLS, 512),
    ("one pool", (("172.17.0.0/12", 16),), 16),
    ("a size equal to the base", (("10.0.0.0/8", 8),), 1),
    # /8 carved at /24 is the widest anyone suggests, and is not what Steward
    # proposes -- pinned so the recommendation's modesty stays visible
    ("a whole private range", (("10.0.0.0/8", 24),), 65536),
    ("nothing configured", (), 0),
    ("a size smaller than its base", (("192.168.0.0/16", 12),), 0),
    ("an unparseable base", (("not-a-network", 24),), 0),
]


@pytest.mark.parametrize(
    ("pools", "expected"),
    [(pools, expected) for _, pools, expected in CAPACITY],
    ids=[case for case, _, _ in CAPACITY],
)
def test_how_many_networks_a_set_of_pools_affords(
    pools: tuple[tuple[str, int], ...], expected: int
) -> None:
    assert AddressPools(pools, configured=True).networks == expected


class FakeDocker:
    """A `docker info` that answers whatever this test needs it to."""

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, *args: Any, **kwargs: Any) -> "FakeDocker":
        return self


def fake_run(monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int = 0) -> None:
    from inspect_steward._launch import pools as module

    monkeypatch.setattr(
        module.subprocess, "run", FakeDocker(stdout, returncode).__call__
    )


def test_an_omitted_field_reads_as_the_built_in_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the whole detection: the daemon carries `DefaultAddressPools` only where
    # somebody configured it, so `null` is an answer rather than a silence
    fake_run(monkeypatch, "null\n")

    read = read_pools()

    assert read is not None
    assert read.configured is False
    assert read.networks == 32


def test_a_configured_daemon_is_read_as_it_reports_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # docker info capitalizes the keys that daemon.json spells lowercase
    fake_run(
        monkeypatch,
        json.dumps([{"Base": "10.0.0.0/8", "Size": 24}]),
    )

    read = read_pools()

    assert read is not None
    assert read.configured is True
    assert read.networks == 65536


def test_no_docker_is_no_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    # a machine with no docker has no ceiling to raise, and a launch on one
    # must not be told to go and edit a daemon it does not have
    from inspect_steward._launch import pools as module

    def missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(module.subprocess, "run", missing)

    assert read_pools() is None
    assert advise(wanted=128) is None


def test_a_daemon_that_will_not_answer_is_no_advice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run(monkeypatch, "", returncode=1)

    assert read_pools() is None


def test_junk_from_the_daemon_is_no_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    # a shape this version does not know is a reason to stay quiet, never a
    # reason to fail a launch
    fake_run(monkeypatch, "{not json")

    assert read_pools() is None


TRIGGER = [
    # the machines that have the problem, and the ones that do not
    ("a big box on the defaults", 128, "null", True),
    ("a laptop on the defaults", 8, "null", False),
    ("exactly at the ceiling", 32, "null", False),
    ("one over it", 33, "null", True),
    ("a box whose pools were already carved", 128, None, False),
]


@pytest.mark.parametrize(
    ("wanted", "reported", "advised"),
    [(wanted, reported, advised) for _, wanted, reported, advised in TRIGGER],
    ids=[case for case, _, _, _ in TRIGGER],
)
def test_the_comparison_is_the_trigger_not_the_configuration(
    wanted: int,
    reported: str | None,
    advised: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # advising a four-core laptop that its thirty networks are too few would be
    # noise on exactly the machines where the ceiling never binds
    fake_run(
        monkeypatch,
        reported
        if reported is not None
        else json.dumps(
            [{"Base": base, "Size": size} for base, size in RECOMMENDED_POOLS]
        ),
    )

    assert (advise(wanted=wanted) is not None) is advised


def test_the_advice_says_what_the_fix_would_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run(monkeypatch, "null\n")

    advice = advise(wanted=128)

    assert advice is not None
    assert (advice.networks, advice.wanted) == (32, 128)
    assert advice.proposed_networks == 512


def test_writing_the_pools_keeps_the_settings_already_in_the_file(
    tmp_path: Path,
) -> None:
    # Docker Desktop ships a daemon.json carrying `builder` and `experimental`,
    # and replacing it would silently drop settings the person chose
    config = tmp_path / "daemon.json"
    config.write_text(
        json.dumps({"builder": {"gc": {"enabled": True}}, "experimental": False}),
        encoding="utf-8",
    )
    advice = PoolAdvice(networks=32, wanted=128, config=config)

    backup = write_pools(advice)

    written = json.loads(config.read_text(encoding="utf-8"))
    assert written["builder"] == {"gc": {"enabled": True}}
    assert written["experimental"] is False
    assert written["default-address-pools"] == [
        {"base": base, "size": size} for base, size in RECOMMENDED_POOLS
    ]
    assert backup is not None and json.loads(backup.read_text(encoding="utf-8")) == {
        "builder": {"gc": {"enabled": True}},
        "experimental": False,
    }


def test_writing_into_a_directory_that_does_not_exist_yet(tmp_path: Path) -> None:
    # a machine that has never had a daemon.json is the ordinary case on Linux
    config = tmp_path / "docker" / "daemon.json"

    backup = write_pools(PoolAdvice(networks=32, wanted=128, config=config))

    assert backup is None
    assert "default-address-pools" in json.loads(config.read_text(encoding="utf-8"))


def test_a_file_that_is_not_json_is_shown_rather_than_overwritten(
    tmp_path: Path,
) -> None:
    config = tmp_path / "daemon.json"
    config.write_text("{ this was hand-edited", encoding="utf-8")

    with pytest.raises(ValueError):
        write_pools(PoolAdvice(networks=32, wanted=128, config=config))

    assert config.read_text(encoding="utf-8") == "{ this was hand-edited"


def test_a_file_holding_a_json_array_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "daemon.json"
    config.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        write_pools(PoolAdvice(networks=32, wanted=128, config=config))


def test_the_platform_answers_are_stated_rather_than_guessed() -> None:
    # both are read by a person and pasted into a shell, so the only thing
    # asserted is that they are answers rather than empty
    assert daemon_config_path().name == "daemon.json"
    assert restart_command()
