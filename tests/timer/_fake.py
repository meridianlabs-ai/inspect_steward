"""A scheduler's system, faked at the one seam every backend goes through.

Backends reach launchctl, systemctl, and crontab through a `Runner`, so a test
supplies one and asserts the argv rather than what the argv did. Nothing here
simulates a scheduler: what is being checked is that Steward asks the right
thing, which is the only half of the exchange Steward is responsible for.

`FakeCrontab` is the one exception, and only barely. Cron's whole interface is
*read the file, write the file*, so what it fakes is the file rather than the
dispatcher — which is what lets a test arm and then ask whether it is armed
without writing into the crontab of whoever is running the suite.
"""

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib import import_module

import pytest
from inspect_steward._timer import Completed
from inspect_steward._timer.cron import NO_CRONTAB


@dataclass
class FakeRunner:
    """Records every command, and answers from a table keyed on a substring."""

    answers: dict[str, Completed] = field(default_factory=dict[str, Completed])
    """Matched against the joined argv, first hit winning. Anything unmatched succeeds silently, which is what most of these commands do."""

    calls: list[list[str]] = field(default_factory=list[list[str]])
    stdin: list[str | None] = field(default_factory=list[str | None])

    def __call__(self, argv: Sequence[str], stdin: str | None = None) -> Completed:
        self.calls.append(list(argv))
        self.stdin.append(stdin)
        joined = " ".join(argv)
        for pattern, answer in self.answers.items():
            if pattern in joined:
                return answer
        return Completed(code=0, output="")

    @property
    def commands(self) -> list[str]:
        """Every call as one string, for asserting that something was asked."""
        return [" ".join(call) for call in self.calls]

    def asked(self, *fragments: str) -> bool:
        """Whether some call contained all of these."""
        return any(
            all(fragment in command for fragment in fragments)
            for command in self.commands
        )


@dataclass
class FakeCrontab:
    """The one file cron gives a user, held in memory.

    Enough of cron for state to survive from an `arm` to the `armed` probe that
    follows it, which no stateless runner can do — and nothing more: it does not
    fire anything, because whether cron honours a line it accepted is cron's
    contract rather than Steward's.
    """

    text: str | None = None
    """The crontab, or `None` for a user who has never had one."""

    def __call__(self, argv: Sequence[str], stdin: str | None = None) -> Completed:
        match list(argv):
            case ["crontab", "-l"]:
                if self.text is None:
                    return Completed(code=NO_CRONTAB, output="crontab: no crontab")
                return Completed(code=0, output=self.text.strip())
            case ["crontab", "-"]:
                self.text = stdin or ""
                return Completed(code=0, output="")
            case _:
                return Completed(code=0, output="")


def fake_cron(monkeypatch: pytest.MonkeyPatch) -> FakeCrontab:
    """Cron, with its one file in memory rather than on the machine.

    For a caller that does not thread a `Runner` — the CLI, and anything else
    exercising the path a shell actually takes. Patched where `_timer.arm`
    resolved `run_command`, which is reached through `import_module` because the
    package re-exports the `arm` *function* under that name and so shadows the
    module a dotted string would find.
    """
    fake = FakeCrontab()
    monkeypatch.setattr(
        import_module("inspect_steward._timer.arm"), "run_command", fake
    )
    crontab_present(monkeypatch)
    return fake


def crontab_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put `crontab` on PATH, whatever machine the suite is running on.

    `Cron.usable` checks for the binary before it shells out, so without this a
    container that has no cron would read as *cron declined this interval* — and
    the interval is what most of these tests are about.
    """
    real = shutil.which

    def which(
        cmd: str, mode: int = os.F_OK | os.X_OK, path: str | None = None
    ) -> str | None:
        return "/usr/bin/crontab" if cmd == "crontab" else real(cmd, mode, path)

    monkeypatch.setattr("inspect_steward._timer.cron.shutil.which", which)


def fails(output: str = "no", code: int = 1) -> Completed:
    return Completed(code=code, output=output)


def succeeds(output: str = "") -> Completed:
    return Completed(code=0, output=output)
