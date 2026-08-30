"""Options more than one command carries, so the three spellings cannot drift.

Every setting Steward owns can be said three ways — in `_steward.yaml`, in a `STEWARD_*` variable, or on the command line — and the rule that makes that safe is that all three go through one parser. `Setting` is the command line's end of it: click hands the raw text to `parse_setting`, which is the same `yaml.safe_load` plus field validation the file and the environment get, so `--samples-ramp true` is refused with the same sentence about saying nothing about how far.

**The variables are named in help text rather than bound with click's `envvar=`, and that is not an oversight.** Binding them would make click a second reader of the same variable, with its own coercion and its own error wording — `STEWARD_MAX_WORKERS=yes` would fail as a click usage error against `IntRange` where `max_workers: yes` fails with a sentence naming what YAML did to it. Two parsers that agree by coincidence is the arrangement this module exists to prevent, so the environment is read in exactly one place (`_workspace.directives._environment`) and these options carry only what a person types.

Refusing at the door rather than inside a turn is deliberate and matches `IntRange`: a value that cannot mean anything is a usage error, and a usage error should cost exit code 2 and a line of help rather than a half-run command.
"""

from collections.abc import Callable, Mapping
from typing import Any

import click

# the overrides model is a versioned wire format, deliberately not public API
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides

from .._workspace import (
    PREFIX,
    VARIABLES,
    DirectivesError,
    parse_override,
    parse_setting,
)


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
        # blank text is the one thing the shared parser cannot refuse, because
        # YAML reads it as `null` and *no preference* is a legitimate value in
        # the file. Typing a flag and leaving it empty is a different act from
        # not typing it, and the only reading of it is a mistake -- the same
        # distinction `_environment` draws in the other direction, where an
        # exported-but-empty variable is a shell profile rather than an
        # instruction
        if not value.strip():
            self.fail("was given an empty value", param, ctx)
        try:
            return parse_setting(self.key, value)
        except DirectivesError as ex:
            self.fail(str(ex), param, ctx)


def overrides(key: str) -> str:
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
            f"`false` to fix it. {overrides('samples_ramp')}"
        ),
    )(f)
    f = click.option(
        "--stall-after",
        type=click.IntRange(min=1),
        default=None,
        help=(
            "Fruitless respawns before a task is given up on. "
            f"{overrides('stall_after')}"
        ),
    )(f)
    f = click.option(
        "--max-workers",
        type=click.IntRange(min=1),
        default=None,
        help=(
            "Worker processes, or unset for a process per task. "
            f"{overrides('max_workers')}"
        ),
    )(f)
    return f


class Override(click.ParamType):
    """One of inspect's eval-set arguments typed on Steward's command line.

    `Setting`'s twin, one vocabulary over: the same `yaml.safe_load` plus the field's own validation, so `--epochs yes` is refused here rather than reaching a worker as `True`, and a value refused on the command line is refused identically in `STEWARD_EPOCHS` and `INSPECT_EVAL_EPOCHS`.
    """

    name = "value"

    def __init__(self, field: str) -> None:
        self.field = field

    def convert(
        self, value: Any, param: click.Parameter | None, ctx: click.Context | None
    ) -> Any:
        if not isinstance(value, str):
            return value
        if not value.strip():
            self.fail("was given an empty value", param, ctx)
        try:
            return parse_override(self.field, value)
        except DirectivesError as ex:
            self.fail(str(ex), param, ctx)


PASSTHROUGH = "Inspect eval-set options"
"""Heading the generated options sit under in `--help`.

Separate from Steward's own, because the distinction is the whole rule: above the heading are words Steward owns and can be said on every command that acts; below it are inspect's, said once at launch and written verbatim into the document capture and every worker read.
"""


def passthrough_options[F: Callable[..., Any]](f: F) -> F:
    """Inspect's eval-set arguments, one flag each, generated from the override map.

    **Not the synonym problem returning.** `steward tend --max-samples` was Steward minting its own spelling of a word it refused elsewhere, applied per turn, with Steward deciding what it meant. These are inspect's own options, named as inspect names them, taking their help text from inspect's own field docstrings, and written verbatim into the overrides document. Steward is the transport.

    **Generated rather than written out**, so the flag set cannot drift from the model: a field added upstream appears here on the next release without anybody noticing it had to. The name, the type, and the help are all read from the map and the model — there is no third copy of any of them.

    **On `launch` alone.** These are run-wide and persist for the run, which is what the manifest records them for; `tend` and `status` recompute Steward's own settings each turn and never re-decide the run's shape.
    """
    for field in reversed(list(VARIABLES)):
        if field == "log_dir":
            continue
        f = click.option(
            f"--{field.replace('_', '-')}",
            f"override_{field}",
            type=Override(field),
            default=None,
            help=_passthrough_help(field),
        )(f)
    return f


class PassthroughCommand(click.Command):
    """A command whose `--help` separates its own options from inspect's.

    Thirty-seven generated flags in one undifferentiated list would bury the six a person actually types, and would also hide the distinction that matters: which of these Steward decides and which it only carries. So the passthrough flags get their own heading and the rest keep theirs.
    """

    def format_options(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        own: list[tuple[str, str]] = []
        passthrough: list[tuple[str, str]] = []
        for parameter in self.get_params(ctx):
            record = parameter.get_help_record(ctx)
            if record is None:
                continue
            name = getattr(parameter, "name", "") or ""
            (passthrough if name.startswith("override_") else own).append(record)
        if own:
            with formatter.section("Options"):
                formatter.write_dl(own)
        if passthrough:
            with formatter.section(PASSTHROUGH):
                formatter.write_dl(passthrough)


def collect_overrides(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """The passthrough flags as the overrides model spells them.

    Args:
        parameters: The command's own parameters, as click bound them.

    Returns:
        Field name to value, for every passthrough flag that was given.
    """
    return {
        field: parameters[f"override_{field}"]
        for field in VARIABLES
        if parameters.get(f"override_{field}") is not None
    }


def _passthrough_help(field: str) -> str:
    """Inspect's own words for the field, plus every variable it answers to.

    The first sentence of the field's docstring: the rest is reasoning for a reader of the model, and a `--help` entry has room for the claim alone.

    A field inspect spells only as a `--no-` flag gets its negated variable named apart from the rest, because it is not another way of saying the same thing — set, it means `false` whatever it is set to. Listed beside the others it would read as a spelling that takes a value; omitted entirely it would tell a reader inspect has no variable for the field, which is worse.
    """
    described = EvalSetOverrides.model_fields[field].description or ""
    sentence = described.split("\n", 1)[0].strip()
    variable = VARIABLES[field]
    spellings = ", ".join((f"{PREFIX}{field.upper()}", *variable.inspect))
    negated = f" ({variable.negated} to turn it off)" if variable.negated else ""
    return f"{sentence} Also {spellings}{negated}."


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
            f"{overrides('tend_interval')}"
        ),
    )(f)
