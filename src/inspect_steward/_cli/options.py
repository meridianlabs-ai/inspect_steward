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
    ALIASED,
    PREFIX,
    DirectivesError,
    parse_override,
    parse_setting,
    spellings,
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

    `--max-workers` and `--stall-after` are integers; `--samples-ramp` is a range or `false`; `--stuck-after` is a duration with a unit; `--preauthorized` is a mapping.
    """
    f = click.option(
        "--preauthorized",
        type=Setting("preauthorized"),
        default=None,
        help=(
            "Rulings granted in advance: class patterns to dispositions, e.g. "
            "`{'error:ReadTimeout@*': rerun}`, or `false` to decline every "
            f"standing grant for this turn. {overrides('preauthorized')}"
        ),
    )(f)
    f = click.option(
        "--stuck-after",
        type=Setting("stuck_after"),
        default=None,
        help=(
            "Quiet time before a running sample is reported stuck, with a "
            f"unit, e.g. `5h`. {overrides('stuck_after')}"
        ),
    )(f)
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

    `Setting`'s twin, one vocabulary over: the same `yaml.safe_load` plus the field's own validation, so `--epochs yes` is refused here rather than reaching a worker as `True`, and a value refused on the command line is refused identically in `STEWARD_EPOCHS`.

    Inspect's own variables are read by inspect (`resolve_eval_env`) and follow its CLI's syntax rather than this one — `INSPECT_EVAL_LIMIT=10-20` where this takes `--limit '[10, 20]'`. Same value, different surface, and each surface consistent with the rest of itself.
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
    """Inspect's eval-set arguments, one flag each, generated from the overrides model.

    **Not the synonym problem returning.** `steward tend --max-samples` was Steward minting its own spelling of a word it refused elsewhere, applied per turn, with Steward deciding what it meant. These are inspect's own options, named as inspect names them, taking their help text from inspect's own field docstrings, and written verbatim into the overrides document. Steward is the transport.

    **Generated rather than written out**, so the flag set cannot drift from the model: a field added upstream appears here on the next release without anybody noticing it had to. The name, the type, the help, and the variables the help names are all read from upstream — there is no second copy of any of them here.

    **On `launch` alone.** These are run-wide and persist for the run, which is what the manifest records them for; `tend` and `status` recompute Steward's own settings each turn and never re-decide the run's shape.
    """
    for field in reversed(ALIASED):
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


def collect_overrides(parameters: Mapping[str, Any]) -> dict[str, Any] | None:
    """The passthrough flags as the overrides model spells them.

    Args:
        parameters: The command's own parameters, as click bound them.

    Returns:
        Field name to value, for every passthrough flag that was given, or `None` where none was. The two are different instructions: `None` says *nothing was typed*, which lets a re-launch reuse what the committed manifest recorded, where an empty mapping says *the definition's own shape* and displaces it.
    """
    given = {
        field: parameters[f"override_{field}"]
        for field in ALIASED
        if parameters.get(f"override_{field}") is not None
    }
    return given or None


def _passthrough_help(field: str) -> str:
    """Inspect's own words for the field, plus every variable it answers to.

    The first sentence of the field's docstring: the rest is reasoning for a reader of the model, and a `--help` entry has room for the claim alone.

    The variables come from `spellings`, which asks upstream rather than keeping a list — so a `--help` entry cannot promise a variable that does not work, and cannot miss one that does. A field inspect declines to read from the environment at all (`notification`, and `log_images` where only `eval-retry` binds one) is left naming its `STEWARD_*` alias alone, which is the truth for it.
    """
    described = EvalSetOverrides.model_fields[field].description or ""
    sentence = described.split("\n", 1)[0].strip()
    return f"{sentence} Also {', '.join(spellings(field))}."


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


def sync_options[F: Callable[..., Any]](f: F) -> F:
    """Where the workspace propagates to, on the commands that propagate it.

    `launch` and `tend`, which are the two verbs that run a turn and therefore the two that write anything out. Not `status`, which writes nothing at all — a read verb that quietly pushed a workspace to a bucket would be exactly the surprise `status` promises not to be.
    """
    f = click.option(
        "--no-sync",
        is_flag=True,
        default=False,
        help="Leave the workspace on this machine, whatever this project configured.",
    )(f)
    return click.option(
        "--sync",
        type=Setting("sync"),
        default=None,
        metavar="PATH|auto",
        help=(
            "Where to mirror this workspace's own files. Defaults to the run's "
            f"log directory, so results and what explains them sit together. "
            f"{overrides('sync')}"
        ),
    )(f)
