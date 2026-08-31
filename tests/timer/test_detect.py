"""Which timer this machine gets, and what each backend asks the system for.

Two things this file can check and nothing else can. **The fall-through**: a
mac gets launchd, a Linux box with a live user manager gets systemd, a machine
with only cron gets cron, and a machine with none of the three is told so — with
cron declining an interval it cannot express. And **the argv**: the activation
half of a backend is never run here, so what is asserted is the command it
built.

The commands themselves are not simulated. What Steward is responsible for is
asking the right thing; whether launchd then does it is launchd's contract, and
a fake that pretended to keep it would be testing the fake.
"""

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from inspect_steward._timer import (
    Completed,
    Cron,
    Launchd,
    Scheduler,
    Systemd,
    TimerEntry,
    TimerError,
    detect,
    scheduler,
    timer_entry,
)

from ._fake import FakeRunner, fails, succeeds


@pytest.fixture
def entry(tmp_path: Path) -> TimerEntry:
    return timer_entry(tmp_path, 600, output=tmp_path / ".steward" / "timer.log")


def on(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    *,
    has: tuple[str, ...] = ("launchctl", "systemctl", "crontab"),
) -> None:
    """Pretend to be a machine, with a given set of scheduler binaries on PATH."""

    def which(name: str, *args: object, **kwargs: object) -> str | None:
        return f"/usr/bin/{name}" if name in has else None

    for module in ("launchd", "systemd", "cron"):
        monkeypatch.setattr(f"inspect_steward._timer.{module}.shutil.which", which)
    for module in ("launchd", "systemd"):
        monkeypatch.setattr(f"inspect_steward._timer.{module}.sys.platform", platform)


# --- the fall-through ---------------------------------------------------


def test_a_mac_gets_launchd(monkeypatch: pytest.MonkeyPatch, entry: TimerEntry) -> None:
    on(monkeypatch, "darwin")

    assert detect(entry, runner=FakeRunner()).name == "launchd"


def test_a_linux_box_with_a_live_user_manager_gets_systemd(
    monkeypatch: pytest.MonkeyPatch, entry: TimerEntry
) -> None:
    on(monkeypatch, "linux")

    assert detect(entry, runner=FakeRunner()).name == "systemd"


def test_systemd_present_but_not_running_is_not_systemd(
    monkeypatch: pytest.MonkeyPatch, entry: TimerEntry
) -> None:
    # a container or WSL has the binary and no user manager for `--user` to
    # talk to, and an installed timer there never fires
    on(monkeypatch, "linux")
    runner = FakeRunner({"show-environment": fails("Failed to connect to bus")})

    assert detect(entry, runner=runner).name == "cron"


def test_a_machine_with_only_cron_gets_cron(
    monkeypatch: pytest.MonkeyPatch, entry: TimerEntry
) -> None:
    on(monkeypatch, "linux", has=("crontab",))

    assert detect(entry, runner=FakeRunner()).name == "cron"


def test_a_machine_with_nothing_is_told_so_rather_than_given_a_substitute(
    monkeypatch: pytest.MonkeyPatch, entry: TimerEntry
) -> None:
    """The reason detection is allowed to fail at all.

    An earlier version had a fourth backend that always claimed to be usable, so
    this branch was unreachable and a bare container was quietly given a timer
    that died with the terminal. Being told is the better outcome: an
    unsupervised run should look unsupervised.
    """
    on(monkeypatch, "linux", has=())

    with pytest.raises(TimerError, match="launchd, systemd, cron"):
        detect(entry, runner=FakeRunner())


def test_cron_without_the_binary_declines_rather_than_erroring(
    monkeypatch: pytest.MonkeyPatch, entry: TimerEntry
) -> None:
    # `run_command` raises when a binary is absent rather than returning an exit
    # code, so a `usable` that shelled out first would escape detection as an
    # error on the machine that most needs to be told what it does not have
    on(monkeypatch, "linux", has=())

    def absent(argv: Sequence[str], stdin: str | None = None) -> Completed:
        raise TimerError("could not run crontab: [Errno 2] No such file")

    assert not Cron(absent).usable(entry)


def test_an_interval_cron_cannot_express_leaves_cron_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # detection is per-entry rather than per-machine for exactly this: the same
    # box takes cron for ten minutes and has nothing to offer for seven
    on(monkeypatch, "linux", has=("crontab",))
    awkward = timer_entry(tmp_path, 420, output=tmp_path / "timer.log")

    with pytest.raises(TimerError):
        detect(awkward, runner=FakeRunner())


def test_a_scheduler_steward_does_not_know_is_named_rather_than_guessed() -> None:
    with pytest.raises(TimerError, match="launchd, systemd, cron"):
        scheduler("anacron")


# --- what each backend asks the system for ------------------------------


def test_launchd_bootstraps_the_agent_it_just_wrote(entry: TimerEntry) -> None:
    runner = FakeRunner()
    backend = Launchd(runner)

    backend.arm(entry)

    plist = backend.plist(entry)
    assert plist.exists()
    assert runner.asked("launchctl", "bootstrap", str(plist))
    # unconditionally first, because bootstrapping over a loaded label fails
    assert runner.commands[0].startswith("launchctl bootout")


def test_launchd_leaves_no_plist_behind_when_it_will_not_load(
    entry: TimerEntry,
) -> None:
    # a file in LaunchAgents that launchd rejected is not a timer, and leaving
    # it would make `timer status` report supervision that does not exist
    runner = FakeRunner({"bootstrap": fails("Load failed: 5: Input/output error")})
    backend = Launchd(runner)

    with pytest.raises(TimerError, match="Input/output error"):
        backend.arm(entry)

    assert not backend.plist(entry).exists()


def test_launchd_disarming_removes_the_plist_and_the_loaded_label(
    entry: TimerEntry,
) -> None:
    runner = FakeRunner()
    backend = Launchd(runner)
    backend.arm(entry)

    backend.disarm(entry)

    assert not backend.plist(entry).exists()
    assert runner.asked("launchctl", "bootout", entry.label)


def test_launchd_disarming_something_never_loaded_is_not_a_failure(
    entry: TimerEntry,
) -> None:
    # the state a disarm wanted, and `arm` calls this to be idempotent. The
    # `print` probe is what separates this from the case below
    backend = Launchd(
        FakeRunner(
            {
                "bootout": fails("Boot-out failed: 3: No such process"),
                "print": fails("Could not find service"),
            }
        )
    )

    backend.disarm(entry)


def test_launchd_disarming_says_so_when_the_agent_is_still_loaded(
    entry: TimerEntry,
) -> None:
    # a disarm that returns quietly while launchd still holds the label means
    # every later `timer status` reports an unarmed run that goes on tending,
    # which is the one direction this must not fail in
    backend = Launchd(
        FakeRunner(
            {
                "bootout": fails("Boot-out failed: 5: Input/output error"),
                "print": succeeds("state = running"),
            }
        )
    )

    with pytest.raises(TimerError, match="still.*loaded"):
        backend.disarm(entry)


def test_systemd_writes_both_units_and_enables_the_timer(entry: TimerEntry) -> None:
    runner = FakeRunner()
    backend = Systemd(runner)

    backend.arm(entry)

    service, timer = backend.units(entry)
    assert service.exists() and timer.exists()
    assert runner.asked("systemctl", "--user", "daemon-reload")
    assert runner.asked("systemctl", "--user", "enable", "--now", timer.name)


def test_systemd_leaves_no_units_behind_when_it_will_not_enable(
    entry: TimerEntry,
) -> None:
    runner = FakeRunner({"enable": fails("Unit is masked")})
    backend = Systemd(runner)

    with pytest.raises(TimerError, match="masked"):
        backend.arm(entry)

    assert not any(unit.exists() for unit in backend.units(entry))


def test_systemd_disarming_removes_both_units(entry: TimerEntry) -> None:
    runner = FakeRunner()
    backend = Systemd(runner)
    backend.arm(entry)

    backend.disarm(entry)

    assert not any(unit.exists() for unit in backend.units(entry))
    assert runner.asked("systemctl", "--user", "disable", "--now")


PROBES: list[tuple[str, Callable[[FakeRunner], Scheduler], dict[str, Completed]]] = [
    ("launchd", Launchd, {}),
    ("systemd", Systemd, {}),
    ("cron", Cron, {"crontab -l": succeeds("")}),
]


@pytest.mark.parametrize(
    ("build", "answers"),
    [(build, answers) for _, build, answers in PROBES],
    ids=[name for name, _, _ in PROBES],
)
def test_a_probe_is_one_question(
    build: "Callable[[FakeRunner], Scheduler]",
    answers: dict[str, Completed],
    entry: TimerEntry,
) -> None:
    # `timer status` is the only reader that pays for this, so it has to stay
    # one subprocess rather than a survey -- every other reader goes on the
    # journal, which is what keeps a tend cheap
    runner = FakeRunner(answers)

    build(runner).armed(entry)

    assert len(runner.calls) == 1
