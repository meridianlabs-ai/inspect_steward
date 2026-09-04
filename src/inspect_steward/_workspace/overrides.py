"""Inspect's words, reaching a Steward run the way inspect's own CLI would.

`_steward.yaml` holds only words `eval_set()` does not know, and Steward's command line holds only words `_steward.yaml` holds (`directives.py`). That rule leaves a real gap: **there was no way to shape one run without editing the definition.** `epochs`, `limit`, `max_samples` — the things a rehearsal changes and a committed file should not — had nowhere to be said.

**A Steward run is an `inspect eval-set` invocation with a longer lifetime**, and that is what closes the gap without reopening the rule. Reading the environment that CLI documents completes a contract Steward stands in for; minting `steward tend --max-samples` was Steward inventing a synonym for a word it had refused a week earlier. So inspect's words arrive as inspect's variables, are passed to inspect's own overrides document unchanged, and Steward is the transport rather than the author.

**Inspect reads its own variables, and that is not a delegation of convenience.** `resolve_eval_env` upstream is the single reader of `INSPECT_*`, because those names are click *inputs* rather than values: an option's type, its callback, and a normalisation block in the command body all run before a value reaches `eval_set()`. A reading built here from the field names agreed with `inspect eval` about two thirds of the time and failed silently the rest — `INSPECT_EVAL_LIMIT=10-20` refused where inspect accepts it, `INSPECT_EVAL_SAMPLE_ID=a,b` quietly becoming one id, and `INSPECT_EVAL_SCORE_DISPLAY` meaning the *opposite* of what its name says. Steward now asks upstream what the environment says and does not have an opinion of its own.

**Steward's half is the `STEWARD_*` alias, and the difference is scope rather than taste.** Inspect's variable is broad by construction: exported in a shell it reaches every `inspect eval` there, and written into a workspace `.env` it reaches every direct inspect run in that directory. `STEWARD_MAX_SAMPLES` is the same knob said quietly — narrowed to this tool — which is the narrowness the removed flag had and the variable alone would lose. It wins where both are set, because a narrower instruction is the more specific one. Aliases go through Steward's own parser, the one `_steward.yaml` and every flag use, so a value refused in one Steward spelling is refused in all of them.

**`log_dir` is Steward's alone.** It has no alias and no flag, because a run's log directory is where the fleet is watched from — a worker writing somewhere else is a worker whose logs no tend reads. `INSPECT_LOG_DIR` is refused at launch rather than ignored, for the reason every unread setting is; upstream declines to read it too, for its own version of the same reason.
"""

from collections.abc import Mapping
from typing import Any

import click
import yaml

# the overrides model and its environment reader are a versioned wire format
# and its parser, deliberately not public API
from inspect_ai._eval.eval_set_env import ENV_VARIABLES, resolve_eval_env
from inspect_ai._eval.eval_set_overrides import (
    EvalSetOverrides,
    check_eval_set_overrides,
    merge_eval_set_overrides,
)
from inspect_ai._util.error import PrerequisiteError
from pydantic import ValidationError

from .directives import PREFIX, STEWARDS, DirectivesError, explain

LOG_DIR = "INSPECT_LOG_DIR"
"""Inspect's log directory variable, refused rather than read.

Every other variable is honoured because Steward is standing in for the CLI that documents it. This one is not, because Steward has already answered the question it asks: the run's logs go where the fleet is watched from, and a variable that quietly moved them would leave every tend reading an empty directory and respawning work that is running.
"""

ALIASED: tuple[str, ...] = tuple(
    field for field in EvalSetOverrides.model_fields if field not in STEWARDS
)
"""Every override field that gets a `STEWARD_*` alias and a `launch` flag.

Derived from the model rather than listed, so a field added upstream is sayable here on the next release without anybody noticing it had to. The exclusions are `STEWARDS`, and they are excluded because Steward decides them.
"""


def spellings(field: str) -> tuple[str, ...]:
    """Every variable this field answers to, narrowest first.

    Steward's alias, then whatever inspect's CLI binds — which is upstream's answer rather than a copy of it, so help text cannot claim a variable that does not work or miss one that does.

    Args:
        field: The field's name, as `EvalSetOverrides` spells it.

    Returns:
        Variable names, `STEWARD_*` first.
    """
    variable = ENV_VARIABLES.get(field)
    return (f"{PREFIX}{field.upper()}", *(variable.names if variable else ()))


def read_overrides(
    environ: Mapping[str, str], given: Mapping[str, Any] = {}
) -> EvalSetOverrides | None:
    """Resolve inspect's words for this run, most specific first.

    The command line, then `STEWARD_X`, then inspect's own variables as inspect itself reads them, then silence — which leaves the definition's value in place, since an omitted field is what *keep what the definition chose* looks like all the way down.

    Args:
        environ: The environment to read.
        given: Values from the command line, already parsed, keyed by field name. `None` values are treated as absent.

    Returns:
        The overrides in force, or `None` where nothing named one.

    Raises:
        DirectivesError: A value is not valid YAML, or is not allowed for its field.
    """
    # inspect's own reading of its own names, translated into Steward's error
    # type so that a bad `INSPECT_EVAL_*` degrades a tend exactly as a bad
    # `_steward.yaml` does rather than escaping as an unhandled exception.
    #
    # `PrerequisiteError` is what upstream raises deliberately, and the other
    # two are what leaks when a value gets past its converter and fails later.
    # `ClickException` is reached today, from the config-file and spec readers
    # the generate-config pass uses; `ValidationError` is not, and is caught
    # anyway because the line between the two is upstream's converter table
    # rather than anything Steward controls. Catching only the deliberate one
    # left a mistyped variable crashing the 02:00 tend, which is the one moment
    # nobody is there to read the traceback
    try:
        inspect_side = resolve_eval_env(environ)
    except (PrerequisiteError, ValidationError, click.ClickException) as ex:
        raise DirectivesError(_message(ex)) from ex

    settings: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for field in ALIASED:
        name = f"{PREFIX}{field.upper()}"
        if (value := _value(environ, name)) is not None:
            settings[field] = _loaded(name, value)
            sources[field] = name
    settings.update(
        {field: value for field, value in given.items() if value is not None}
    )

    steward_side = _validated(settings, sources) if settings else None
    if inspect_side is None:
        return steward_side
    if steward_side is None:
        return inspect_side
    # narrower over broader, field by field
    return merge_eval_set_overrides(inspect_side, steward_side)


def parse_override(field: str, value: str) -> Any:
    """One override typed on the command line, read as `_steward.yaml` reads a setting.

    The same two steps every *Steward* spelling goes through — `yaml.safe_load` for what the text means, then the field's own validation for whether it is allowed — so `--epochs yes` is refused here rather than reaching a worker as `True`.

    Deliberately not inspect's CLI syntax. `--limit 10-20` is inspect's spelling of a range and `--limit '[10, 20]'` is Steward's, and a flag on `steward launch` sits beside `--samples-ramp '[40, 200]'` rather than beside `inspect eval`. The value is the same either way; only the surface an operator is typing at differs.

    Args:
        field: The field's name, as `EvalSetOverrides` spells it.
        value: What was typed.

    Returns:
        The validated value.

    Raises:
        DirectivesError: The text is not valid YAML, or the value is not allowed for this field.
    """
    loaded = _loaded(field, value)
    try:
        overrides = EvalSetOverrides.model_validate({field: loaded})
    except ValidationError as ex:
        raise DirectivesError(explain(ex)) from ex
    if (found := check_eval_set_overrides(overrides)) is not None:
        offending, detail = found
        raise DirectivesError(f"`{offending}` has {detail}")
    return getattr(overrides, field)


def _validated(
    settings: dict[str, Any], sources: dict[str, str]
) -> EvalSetOverrides | None:
    try:
        overrides = EvalSetOverrides.model_validate(settings)
    except ValidationError as ex:
        raise DirectivesError(explain(ex, sources)) from ex

    # the second half of the check, and it has to be separate: a range of two
    # equal numbers is a well-typed empty slice, so the model accepts it and
    # only inspect knows it means nothing
    if (found := check_eval_set_overrides(overrides)) is not None:
        field, detail = found
        raise DirectivesError(f"{sources.get(field, field)} has {detail}")
    return overrides


def _message(ex: Exception) -> str:
    """Inspect's refusal, without the console markup its own display strips.

    A `ValidationError` is rendered by pydantic and reaches here as several
    lines; that is worse than `explain` would do but better than a traceback,
    and the shape it takes is upstream's to improve rather than Steward's to
    reformat.
    """
    text = str(ex).replace("[bold]", "").replace("[/bold]", "")
    return text.removeprefix("ERROR: ")


def _value(environ: Mapping[str, str], name: str) -> str | None:
    """A variable's text, treating exported-but-empty as unset.

    The same reading `_timer.env` gives a credential and `directives._environment` gives a `STEWARD_*` setting: refusing a run because somebody's shell profile exports an empty variable would be refusing a correct setup.
    """
    value = environ.get(name)
    return value if value is not None and value.strip() else None


def _loaded(name: str, value: str) -> Any:
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError as ex:
        raise DirectivesError(f"{name} is not a valid value: {ex}") from ex


__all__ = [
    "ALIASED",
    "LOG_DIR",
    "parse_override",
    "read_overrides",
    "spellings",
]
