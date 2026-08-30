"""Options more than one command carries, so the three spellings cannot drift.

Every setting Steward owns can be said three ways — in `_steward.yaml`, in a `STEWARD_*` variable, or on the command line — and the rule that makes that safe is that all three go through one parser. `Setting` is the command line's end of it: click hands the raw text to `parse_setting`, which is the same `yaml.safe_load` plus field validation the file and the environment get, so `--samples-ramp true` is refused with the same sentence about saying nothing about how far.

**The variables are named in help text rather than bound with click's `envvar=`, and that is not an oversight.** Binding them would make click a second reader of the same variable, with its own coercion and its own error wording — `STEWARD_MAX_WORKERS=yes` would fail as a click usage error against `IntRange` where `max_workers: yes` fails with a sentence naming what YAML did to it. Two parsers that agree by coincidence is the arrangement this module exists to prevent, so the environment is read in exactly one place (`_workspace.directives._environment`) and these options carry only what a person types.

Refusing at the door rather than inside a turn is deliberate and matches `IntRange`: a value that cannot mean anything is a usage error, and a usage error should cost exit code 2 and a line of help rather than a half-run command.
"""

from collections.abc import Callable
from typing import Any

import click

from .._workspace import PREFIX, DirectivesError, parse_setting


class Setting(click.ParamType):
    """A `_steward.yaml` value typed on the command line.

    Used where a setting is richer than an integer — a range, or a duration with a unit. Plain integer settings keep `click.IntRange`, which already produces the right refusal and needs no help from here.
    """

    name = "value"

    def __init__(self, key: str) -> None:
        self.key = key

    def convert(
        self, value: Any, param: click.Parameter | None, ctx: click.Context | None
    ) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return parse_setting(self.key, value)
        except DirectivesError as ex:
            self.fail(str(ex), param, ctx)


def _overrides(key: str) -> str:
    """The sentence every one of these options ends with.

    Both other spellings named in one place, because a flag that mentions the file but not the variable teaches half the rule.
    """
    return f"Overrides `{key}` in `_steward.yaml` and `{PREFIX}{key.upper()}`."


def shape_options[F: Callable[..., Any]](f: F) -> F:
    """The settings that shape one turn, on every command that runs or previews one.

    `--max-workers` and `--stall-after` are integers; `--samples-ramp` is a range or `false`.
    """
    f = click.option(
        "--samples-ramp",
        type=Setting("samples_ramp"),
        default=None,
        help=(
            "Range to discover sample concurrency in, e.g. `[40, 300]`, or "
            f"`false` to fix it. {_overrides('samples_ramp')}"
        ),
    )(f)
    f = click.option(
        "--stall-after",
        type=click.IntRange(min=1),
        default=None,
        help=(
            "Fruitless respawns before a task is given up on. "
            f"{_overrides('stall_after')}"
        ),
    )(f)
    f = click.option(
        "--max-workers",
        type=click.IntRange(min=1),
        default=None,
        help=(
            "Worker processes, or unset for a process per task. "
            f"{_overrides('max_workers')}"
        ),
    )(f)
    return f


def tend_interval_option[F: Callable[..., Any]](f: F) -> F:
    """How often a scheduled tend runs, on the commands that install one.

    Not on `tend` or `status`, which run a single turn and have no cadence to set — the rule is a flag on every command that can *act* on the setting, not a flag everywhere.
    """
    return click.option(
        "--tend-interval",
        type=Setting("tend_interval"),
        default=None,
        help=(
            "How often a scheduled tend runs, with a unit, e.g. `10m`. "
            f"{_overrides('tend_interval')}"
        ),
    )(f)
