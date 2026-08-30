"""Inspect's words, reaching a Steward run the way inspect's own CLI would.

`_steward.yaml` holds only words `eval_set()` does not know, and Steward's command line holds only words `_steward.yaml` holds (`directives.py`). That rule leaves a real gap: **there was no way to shape one run without editing the definition.** `epochs`, `limit`, `max_samples` — the things a rehearsal changes and a committed file should not — had nowhere to be said.

**A Steward run is an `inspect eval-set` invocation with a longer lifetime**, and that is what closes the gap without reopening the rule. Reading the environment that CLI documents completes a contract Steward stands in for; minting `steward tend --max-samples` was Steward inventing a synonym for a word it had refused a week earlier. So inspect's words arrive as inspect's variables, are passed to inspect's own overrides document unchanged, and Steward is the transport rather than the author.

**Every one also has a `STEWARD_*` alias, and the difference is scope rather than taste.** Inspect's variable is broad by construction: exported in a shell it reaches every `inspect eval` there, and written into a workspace `.env` it reaches every direct inspect run in that directory. `STEWARD_MAX_SAMPLES` is the same knob said quietly — narrowed to this tool — which is the narrowness the removed flag had and the variable alone would lose. It wins where both are set, because a narrower instruction is the more specific one.

**The names are data, not a rule.** `log_shared` answers to two variables, `log_format` to two others, `log_level` and `display` drop the `EVAL_` infix entirely, and four fields have only a *negated* variable because inspect's CLI spells them as `--no-` flags. Deriving `INSPECT_EVAL_{FIELD}` would be right about two thirds of the time, which is the worst possible accuracy for something that fails silently. `VARIABLES` is therefore written out, and `test_overrides.py` asserts its keys are exactly `EvalSetOverrides`' fields so a field added upstream cannot arrive unmapped.

**`log_dir` is Steward's alone.** It has no alias and no inspect spelling here, because a run's log directory is where the fleet is watched from — a worker writing somewhere else is a worker whose logs no tend reads. `INSPECT_LOG_DIR` is refused at launch rather than ignored, for the reason every unread setting is.
"""

from collections.abc import Mapping
from typing import Any, NamedTuple

import yaml

# the overrides model is a versioned wire format, deliberately not public API
from inspect_ai._eval.eval_set_overrides import (
    EvalSetOverrides,
    check_eval_set_overrides,
)
from pydantic import ValidationError

from .directives import PREFIX, DirectivesError, explain

LOG_DIR = "INSPECT_LOG_DIR"
"""Inspect's log directory variable, refused rather than read.

Every other variable here is honoured because Steward is standing in for the CLI that documents it. This one is not, because Steward has already answered the question it asks: the run's logs go where the fleet is watched from, and a variable that quietly moved them would leave every tend reading an empty directory and respawning work that is running.
"""


class Variable(NamedTuple):
    """How one override field is spelled outside Steward."""

    inspect: tuple[str, ...] = ()
    """Inspect's own variable names, most specific first. Empty where inspect has no positive spelling."""

    negated: str | None = None
    """Inspect's negated variable, where its CLI spells the field as a `--no-` flag. Set, it means `false`."""


VARIABLES: dict[str, Variable] = {
    # --- where output goes ---------------------------------------------------
    # `log_dir` is Steward's; see LOG_DIR above
    "log_dir": Variable(),
    "log_format": Variable(("INSPECT_LOG_FORMAT", "INSPECT_EVAL_LOG_FORMAT")),
    "log_samples": Variable(negated="INSPECT_EVAL_NO_LOG_SAMPLES"),
    "log_realtime": Variable(negated="INSPECT_EVAL_NO_LOG_REALTIME"),
    "log_images": Variable(("INSPECT_EVAL_LOG_IMAGES",)),
    "log_model_api": Variable(("INSPECT_EVAL_LOG_MODEL_API",)),
    "log_refusals": Variable(("INSPECT_EVAL_LOG_REFUSALS",)),
    "log_buffer": Variable(("INSPECT_EVAL_LOG_BUFFER",)),
    "log_shared": Variable(("INSPECT_LOG_SHARED", "INSPECT_EVAL_LOG_SHARED")),
    "log_level": Variable(("INSPECT_LOG_LEVEL",)),
    "log_level_transcript": Variable(("INSPECT_LOG_LEVEL_TRANSCRIPT",)),
    # --- how much of the dataset runs ----------------------------------------
    "limit": Variable(("INSPECT_EVAL_LIMIT",)),
    "sample_id": Variable(("INSPECT_EVAL_SAMPLE_ID",)),
    "sample_shuffle": Variable(("INSPECT_EVAL_SAMPLE_SHUFFLE",)),
    "epochs": Variable(("INSPECT_EVAL_EPOCHS",)),
    # --- how fast it runs ----------------------------------------------------
    "max_samples": Variable(("INSPECT_EVAL_MAX_SAMPLES",)),
    "max_tasks": Variable(("INSPECT_EVAL_MAX_TASKS",)),
    "max_subprocesses": Variable(("INSPECT_EVAL_MAX_SUBPROCESSES",)),
    "max_sandboxes": Variable(("INSPECT_EVAL_MAX_SANDBOXES",)),
    "max_dataset_memory": Variable(("INSPECT_EVAL_MAX_DATASET_MEMORY",)),
    "generate_config": Variable(("INSPECT_EVAL_GENERATE_CONFIG",)),
    # --- what the run is made of, other than the evaluation ------------------
    "model_base_url": Variable(("INSPECT_EVAL_MODEL_BASE_URL",)),
    "model_cost_config": Variable(("INSPECT_EVAL_MODEL_COST_CONFIG",)),
    "sandbox": Variable(("INSPECT_EVAL_SANDBOX",)),
    "sandbox_cleanup": Variable(negated="INSPECT_EVAL_NO_SANDBOX_CLEANUP"),
    "sandbox_prebuilt": Variable(("INSPECT_EVAL_SANDBOX_PREBUILT",)),
    "checkpoint": Variable(("INSPECT_EVAL_CHECKPOINT",)),
    "approval": Variable(("INSPECT_EVAL_APPROVAL",)),
    # --- what happens when something goes wrong -------------------------------
    "retry_on_error": Variable(("INSPECT_EVAL_RETRY_ON_ERROR",)),
    "score_on_error": Variable(("INSPECT_EVAL_SCORE_ON_ERROR",)),
    "debug_errors": Variable(("INSPECT_DEBUG_ERRORS",)),
    # --- what the run reports -------------------------------------------------
    "score": Variable(negated="INSPECT_EVAL_NO_SCORE"),
    "score_display": Variable(("INSPECT_EVAL_SCORE_DISPLAY",)),
    "tags": Variable(("INSPECT_EVAL_TAGS",)),
    "metadata": Variable(("INSPECT_EVAL_METADATA",)),
    "notification": Variable(("INSPECT_EVAL_NOTIFICATION",)),
    "display": Variable(("INSPECT_DISPLAY",)),
    "trace": Variable(("INSPECT_EVAL_TRACE",)),
}
"""Every overridable `eval_set()` argument, and what inspect calls it in the environment.

Keys are exactly `EvalSetOverrides.model_fields`, asserted by a test rather than trusted — a field added upstream that nobody maps here would be an override Steward silently cannot pass on.
"""


def read_overrides(
    environ: Mapping[str, str], given: Mapping[str, Any] = {}
) -> EvalSetOverrides | None:
    """Resolve inspect's words for this run, most specific first.

    The command line, then `STEWARD_X`, then inspect's own variable, then silence — which leaves the definition's value in place, since an omitted field is what *keep what the definition chose* looks like all the way down.

    Args:
        environ: The environment to read.
        given: Values from the command line, already parsed, keyed by field name. `None` values are treated as absent.

    Returns:
        The overrides in force, or `None` where nothing named one.

    Raises:
        DirectivesError: A value is not valid YAML, or is not allowed for its field.
    """
    settings: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for field, variable in VARIABLES.items():
        # the alias first, then inspect's own: the narrower instruction is the
        # more specific one, and an exported INSPECT_EVAL_* reaches every direct
        # `inspect eval` in the shell where a STEWARD_* reaches only this
        for name in (f"{PREFIX}{field.upper()}", *variable.inspect):
            if (value := _value(environ, name)) is not None:
                settings[field] = _loaded(name, value)
                sources[field] = name
                break
        else:
            # a negated variable is only ever `false`, and only where inspect's
            # CLI has no positive spelling to read instead
            if variable.negated and _value(environ, variable.negated) is not None:
                settings[field] = False
                sources[field] = variable.negated
    settings.update(
        {field: value for field, value in given.items() if value is not None}
    )
    if not settings:
        return None

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


def parse_override(field: str, value: str) -> Any:
    """One override typed on the command line, read exactly as its variable would be.

    The same two steps every spelling of every setting goes through — `yaml.safe_load` for what the text means, then the field's own validation for whether it is allowed — so `--epochs yes` is refused here rather than reaching a worker as `True`.

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
    "LOG_DIR",
    "VARIABLES",
    "Variable",
    "parse_override",
    "read_overrides",
]
