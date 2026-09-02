"""What a rehearsal rehearses — the inputs it captures under, and the environment its workers get.

**A gate that blesses a configuration it never exercised is worse than no gate**, because it is read as evidence. Three ways that happened, each caught here: capturing the definition's own defaults where the launch will reuse what is committed; letting `--no-overrides` displace the committed manifest while quietly reading `STEWARD_*` straight back in; and settling the scan model and the notification channel *after* the workers had already started, so the fleet ran on the shell's answer while the digest reported the workspace's.

Nothing here spawns. The claim in each case is about what was decided before the first worker started, so the fleet is the part that can be stubbed out.
"""

import os
import shlex
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from inspect_steward._cli.launch import _Given, _next_launch
from inspect_steward._cli.main import steward
from inspect_steward._evalset.manifest import Manifest, write_manifest
from inspect_steward._notify import INSPECT_NOTIFICATION
from inspect_steward._scan.model import SCOUT_SCAN_MODEL
from inspect_steward._smoke import run as run_module
from inspect_steward._smoke.run import Plan, smoke
from inspect_steward._workspace import (
    Held,
    Workspace,
    create_workspace,
    parse_override,
)

from .._logs import SynthTask, synth_manifest
from ..launch._fake import FakeCapture, fake_capture

ADDITION = SynthTask("addition", samples=2)


class Fleet:
    """A spawn that starts nothing and records what the environment held when it was asked."""

    def __init__(self) -> None:
        self.environ: dict[str, str] = {}

    def spawn(
        self,
        workspace: Workspace,
        definition: Path,
        plan: Plan,
        *,
        deadline: float | None = None,
    ) -> list[str]:
        self.environ = dict(os.environ)
        return []


def stubbed(monkeypatch: pytest.MonkeyPatch) -> Fleet:
    """Run everything a rehearsal decides, and none of what it starts."""
    fleet = Fleet()

    def watch(plan: Plan, *, now: float) -> bool:
        return False

    monkeypatch.setattr(run_module, "spawn", fleet.spawn)
    monkeypatch.setattr(run_module, "watch", watch)
    return fleet


def rehearse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    committed: Manifest | None = None,
    **kwargs: Any,
) -> tuple[FakeCapture, Fleet]:
    """Rehearse `ADDITION` against a workspace, spawning nothing.

    Args:
        tmp_path: The workspace root.
        monkeypatch: Pytest's patcher.
        committed: Desired state to write first, or `None` for a workspace that has never launched.
        **kwargs: Passed to `smoke`.

    Returns:
        The capture, whose `calls` say what was captured under, and the fleet, whose `environ` says what the workers would have inherited.
    """
    create_workspace(tmp_path, git=False)
    workspace = Workspace.at(tmp_path)
    if committed is not None:
        write_manifest(committed, workspace.manifest)
    capture = fake_capture(monkeypatch, synth_manifest([ADDITION]))
    fleet = stubbed(monkeypatch)

    result = smoke(workspace, tmp_path / "evalset.py", cap=0, **kwargs)

    assert not isinstance(result, Held)
    return capture, fleet


def with_args(manifest: Manifest, args: dict[str, Any]) -> Manifest:
    """The same manifest, recorded as having been captured with these arguments."""
    return manifest.model_copy(
        update={"source": manifest.source.model_copy(update={"args": args})}
    )


class TestWhatItCapturesUnder:
    """A rehearsal of the launch, rather than of the definition.

    A workspace launched once with `-A` and `--epochs` carries both in its committed manifest, and a later bare `steward launch` reuses them. A rehearsal that captured the definition's own defaults instead would establish that a run nobody is about to make works — and the gate would then compare identifiers across two different eval sets and find them fine.
    """

    def test_the_committed_arguments_are_reused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture, _ = rehearse(
            tmp_path,
            monkeypatch,
            committed=with_args(synth_manifest([ADDITION]), {"split": "test"}),
        )

        assert capture.calls[-1].args == {"split": "test"}

    def test_an_argument_given_here_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture, _ = rehearse(
            tmp_path,
            monkeypatch,
            committed=with_args(synth_manifest([ADDITION]), {"split": "test"}),
            args={"split": "train"},
        )

        assert capture.calls[-1].args == {"split": "train"}

    def test_no_args_asks_for_the_definitions_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # an empty mapping and `None` are different instructions, here as in a
        # launch: this is the way back once a workspace has been launched with
        # arguments
        capture, _ = rehearse(
            tmp_path,
            monkeypatch,
            committed=with_args(synth_manifest([ADDITION]), {"split": "test"}),
            args={},
        )

        assert capture.calls[-1].args == {}

    def test_the_committed_overrides_are_reused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        committed = synth_manifest([ADDITION])
        capture, _ = rehearse(
            tmp_path,
            monkeypatch,
            committed=committed.model_copy(
                update={"overrides": EvalSetOverrides(epochs=4)}
            ),
        )

        recorded = capture.calls[-1].overrides
        assert recorded is not None and recorded.epochs == 4

    def test_no_overrides_displaces_the_environment_as_well(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the flag says *the definition's own shape*, and a rehearsal reading
        # `STEWARD_*` back in underneath does half of what it says while
        # reporting that it did all of it
        monkeypatch.setenv("STEWARD_EPOCHS", "3")

        capture, _ = rehearse(tmp_path, monkeypatch, overrides={})

        assert capture.calls[-1].overrides is None

    def test_the_environment_is_read_when_nothing_says_otherwise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEWARD_EPOCHS", "3")

        capture, _ = rehearse(tmp_path, monkeypatch)

        recorded = capture.calls[-1].overrides
        assert recorded is not None and recorded.epochs == 3


class TestWhatTheWorkersInherit:
    """Settled before the first worker starts, or not settled at all.

    Both values reach the fleet through this process's environment, which is why a tend settles them in its first two lines. Establishing them after the workers had started left them scanning with the shell's answer and unable to reach anybody, while the digest reported the configured one.
    """

    def test_the_scan_model_is_exported_before_anything_spawns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SCOUT_SCAN_MODEL, raising=False)

        _, fleet = rehearse(tmp_path, monkeypatch, scan_model="openai/gpt-5")

        assert fleet.environ.get(SCOUT_SCAN_MODEL) == "openai/gpt-5"

    def test_no_scan_model_clears_an_ambient_one_before_anything_spawns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the one thing only Steward can say: the variable itself has no
        # spelling for *not that*
        monkeypatch.setenv(SCOUT_SCAN_MODEL, "openai/gpt-5")

        _, fleet = rehearse(tmp_path, monkeypatch, scan_model=False)

        assert SCOUT_SCAN_MODEL not in fleet.environ

    def test_the_channel_is_exported_before_anything_spawns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(INSPECT_NOTIFICATION, raising=False)

        _, fleet = rehearse(tmp_path, monkeypatch, notification="json://example.com")

        assert fleet.environ.get(INSPECT_NOTIFICATION) == "json://example.com"

    def test_declining_silences_steward_and_never_the_fleet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # a worker's notifications are blocking prompts, so a sample parked on
        # an approval with nobody reachable holds its slot until morning
        monkeypatch.delenv(INSPECT_NOTIFICATION, raising=False)
        create_workspace(tmp_path, git=False)
        workspace = Workspace.at(tmp_path)
        workspace.directives.write_text(
            "notification: json://example.com\n", encoding="utf-8"
        )
        fake_capture(monkeypatch, synth_manifest([ADDITION]))
        fleet = stubbed(monkeypatch)

        smoke(workspace, tmp_path / "evalset.py", cap=0, notification=False)

        assert fleet.environ.get(INSPECT_NOTIFICATION) == "json://example.com"


class TestTheCommandItPrintsBack:
    """The follow-up a person copies out of the terminal, and the two ways it was not runnable.

    A bare `steward launch` after a first smoke launches at the definition's own shape — there is no committed manifest yet to reuse what the rehearsal was given — so what the rehearsal was shaped by has to be printed back. Both halves of doing that were wrong. Half of it came off a command line and can hold whitespace or a quote, and was concatenated with spaces; the other half was *parsed* on the way in and was printed with `str()`, which for a `(100, 200)` window is not valid input to the flag that produced it.
    """

    def given(
        self,
        *,
        args: tuple[str, ...] = (),
        overrides: dict[str, Any] | None = None,
    ) -> _Given:
        return _Given(
            definition=None,
            args=args,
            no_args=False,
            type=None,
            overrides=overrides,
            no_overrides=False,
            max_workers=None,
            scan_model=None,
            no_scan_model=False,
        )

    def printed(
        self,
        tmp_path: Path,
        *,
        args: tuple[str, ...] = (),
        overrides: dict[str, Any] | None = None,
    ) -> str:
        create_workspace(tmp_path, git=False)
        return _next_launch(
            Workspace.at(tmp_path),
            tmp_path / "evalset.py",
            self.given(args=args, overrides=overrides),
        )

    def test_an_argument_holding_a_space_survives_the_round_trip(
        self, tmp_path: Path
    ) -> None:
        printed = self.printed(tmp_path, args=("prompt=hello world",))

        assert printed == "steward launch -A 'prompt=hello world'"
        assert shlex.split(printed)[-1] == "prompt=hello world"

    def test_a_range_is_spelled_the_way_the_flag_reads_it(self, tmp_path: Path) -> None:
        # `parse_override` is `yaml.safe_load`, so the JSON form round-trips and
        # Python's own repr of the tuple does not parse at all
        printed = self.printed(tmp_path, overrides={"limit": (100, 200)})

        assert printed == "steward launch --limit '[100, 200]'"
        assert parse_override("limit", shlex.split(printed)[-1]) == (100, 200)

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("limit", 10, "10"),
            ("epochs", 3, "3"),
            ("log_model_api", True, "true"),
            ("log_level", "warning", "warning"),
            ("sample_id", [1, 2], "[1, 2]"),
        ],
    )
    def test_every_override_shape_parses_back_to_itself(
        self, tmp_path: Path, field: str, value: Any, expected: str
    ) -> None:
        printed = self.printed(tmp_path, overrides={field: value})

        typed = shlex.split(printed)[-1]
        assert typed == expected
        assert parse_override(field, typed) == value

    def test_nothing_typed_prints_the_bare_command(self, tmp_path: Path) -> None:
        assert self.printed(tmp_path) == "steward launch"


class TestFlagsARehearsalCannotHonour:
    """The mirror of `--samples` outside `--smoke`, and it closed a silent loss rather than a confusion.

    A rehearsal commits nothing, arms nothing and tends nothing, so every launch-shaping option was accepted and dropped — and the follow-up command printed after a passing smoke carried only the flags the rehearsal had *used*. `--smoke --no-timer` therefore printed a bare `steward launch`, which arms one. Naming them beats preserving them: printing back a flag the rehearsal ignored would claim it had been rehearsed under it.
    """

    def refused(self, tmp_path: Path, *flags: str) -> Any:
        create_workspace(tmp_path, git=False)
        definition = tmp_path / "evalset.py"
        definition.write_text("", encoding="utf-8")
        return CliRunner().invoke(
            steward, ["launch", str(definition), "--smoke", *flags]
        )

    @pytest.mark.parametrize(
        "flag",
        [
            "--no-timer",
            "--accept-archive",
            "--no-env-check",
            "--no-sync",
            "--no-log-root",
            "--no-log-store",
        ],
    )
    def test_a_launch_only_flag_is_refused_rather_than_ignored(
        self, tmp_path: Path, flag: str
    ) -> None:
        result = self.refused(tmp_path, flag)

        assert result.exit_code != 0
        assert flag in result.output
        assert "--smoke launches nothing" in result.output

    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            ("--tend-interval", "10m"),
            ("--samples-ramp", "false"),
            ("--stall-after", "4"),
            ("--stuck-after", "9"),
        ],
    )
    def test_a_launch_only_setting_is_too(
        self, tmp_path: Path, flag: str, value: str
    ) -> None:
        result = self.refused(tmp_path, flag, value)

        assert result.exit_code != 0
        assert flag in result.output

    def test_every_one_that_was_given_is_named_at_once(self, tmp_path: Path) -> None:
        # the same discipline every gate in this codebase keeps: fixing one and
        # being met by the next is the loop the whole-list answer collapses
        result = self.refused(tmp_path, "--no-timer", "--no-sync")

        assert "--no-timer" in result.output and "--no-sync" in result.output

    def test_the_flags_a_rehearsal_does_honour_are_untouched(
        self, tmp_path: Path
    ) -> None:
        # `--max-workers`, `--scan-model` and `--notification` all shape what
        # the rehearsal runs, so they must not be swept up by the refusal
        result = self.refused(tmp_path, "--max-workers", "2", "--no-scan-model")

        assert "shape the launch rather than the rehearsal" not in result.output
