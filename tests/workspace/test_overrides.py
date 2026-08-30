"""Inspect's words reaching a Steward run.

Steward's half of this is now small, and the tests follow: **who wins** when the
same field is said two ways, and that Steward's own spellings go through
Steward's own parser. The reading of `INSPECT_*` belongs to
`inspect_ai._eval.eval_set_env`, which is tested against inspect's CLI upstream
— asserting it again here would be a second opinion about somebody else's
contract, and a stale one the moment upstream adds an option.
"""

from typing import Any

import pytest
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from inspect_steward._workspace import (
    ALIASED,
    LOG_DIR,
    DirectivesError,
    parse_override,
    read_overrides,
    spellings,
)


def test_every_overridable_argument_can_be_said_to_steward() -> None:
    # derived from the model rather than listed, so a field added upstream is
    # sayable on the next release without anybody noticing it had to
    assert set(ALIASED) == set(EvalSetOverrides.model_fields) - {"log_dir"}


def test_the_log_directory_is_stewards_alone() -> None:
    # the run's logs go where the fleet is watched from, so there is no scope at
    # which somebody else decides -- no alias, no flag, and the one variable
    # that would say it refused at launch rather than read
    assert "log_dir" not in ALIASED
    assert LOG_DIR == "INSPECT_LOG_DIR"
    assert read_overrides({LOG_DIR: "s3://elsewhere"}) is None


def test_the_spellings_of_a_field_come_from_upstream() -> None:
    """Help text names what inspect actually reads, rather than what Steward guesses.

    `max_samples` has both spellings; `notification` has only Steward's,
    because inspect deliberately does not read its variable as an option value.
    """
    assert spellings("max_samples") == (
        "STEWARD_MAX_SAMPLES",
        "INSPECT_EVAL_MAX_SAMPLES",
    )
    assert spellings("notification") == ("STEWARD_NOTIFICATION",)


RESOLVED: list[tuple[str, dict[str, str], dict[str, Any], str, Any]] = [
    ("nobody said anything", {}, {}, "epochs", None),
    ("inspect's own", {"INSPECT_EVAL_EPOCHS": "3"}, {}, "epochs", 3),
    ("the scoped alias", {"STEWARD_EPOCHS": "3"}, {}, "epochs", 3),
    (
        "both, narrowest first",
        {"INSPECT_EVAL_EPOCHS": "3", "STEWARD_EPOCHS": "5"},
        {},
        "epochs",
        5,
    ),
    ("the command line", {"STEWARD_EPOCHS": "3"}, {"epochs": 9}, "epochs", 9),
    (
        "inspect's syntax, read by inspect",
        {"INSPECT_EVAL_LIMIT": "10-20"},
        {},
        "limit",
        (9, 20),
    ),
    (
        "steward's syntax, read by steward",
        {"STEWARD_LIMIT": "[0, 5]"},
        {},
        "limit",
        (0, 5),
    ),
    (
        "a field inspect only spells negatively",
        {"INSPECT_EVAL_NO_SCORE": "1"},
        {},
        "score",
        False,
    ),
    ("exported but empty", {"STEWARD_MAX_SAMPLES": "  "}, {}, "max_samples", None),
]


@pytest.mark.parametrize(
    ("environ", "given", "field", "expected"),
    [
        (environ, given, field, expected)
        for _, environ, given, field, expected in RESOLVED
    ],
    ids=[case for case, _, _, _, _ in RESOLVED],
)
def test_an_override_resolves_most_specific_first(
    environ: dict[str, str], given: dict[str, Any], field: str, expected: Any
) -> None:
    # the flag is this invocation, the alias is this tool, and inspect's own
    # variable is every `inspect eval` in the shell -- narrower wins, which is
    # the whole reason the alias exists beside a variable that already works
    overrides = read_overrides(environ, given)

    assert (getattr(overrides, field) if overrides else None) == expected


def test_the_two_layers_merge_rather_than_replace() -> None:
    """A `STEWARD_*` for one field must not discard inspect's for another.

    The merge is field by field, so an alias narrows exactly what it names.
    """
    overrides = read_overrides(
        {"INSPECT_EVAL_MAX_SANDBOXES": "6", "STEWARD_EPOCHS": "2"}
    )

    assert overrides is not None
    assert (overrides.max_sandboxes, overrides.epochs) == (6, 2)


def test_silence_is_no_document_at_all() -> None:
    # an empty overrides object and no object are the same instruction -- keep
    # what the definition chose -- and only one of them is worth writing down
    assert read_overrides({}) is None
    assert read_overrides({"PATH": "/usr/bin"}, {"epochs": None}) is None


REFUSED: list[tuple[str, dict[str, str], str]] = [
    ("a coerced count", {"STEWARD_EPOCHS": "yes"}, "STEWARD_EPOCHS"),
    ("a quoted count", {"STEWARD_MAX_SAMPLES": "'8'"}, "STEWARD_MAX_SAMPLES"),
    ("a meaningless range", {"STEWARD_LIMIT": "[5, 5]"}, "STEWARD_LIMIT"),
    (
        "an identity-bearing generate config",
        {"STEWARD_GENERATE_CONFIG": "{temperature: 0.5}"},
        "temperature",
    ),
    # inspect's own refusal, surfaced as Steward's error type so that a bad
    # variable degrades a tend the way a bad `_steward.yaml` does
    (
        "a value inspect itself refuses",
        {"INSPECT_EVAL_MAX_SAMPLES": "lots"},
        "INSPECT_EVAL_MAX_SAMPLES",
    ),
]


@pytest.mark.parametrize(
    ("environ", "says"),
    [(environ, says) for _, environ, says in REFUSED],
    ids=[case for case, _, _ in REFUSED],
)
def test_a_value_that_cannot_mean_anything_names_where_it_came_from(
    environ: dict[str, str], says: str
) -> None:
    # an author staring at a definition that does not contain the offending
    # value needs to be told which variable does, or the message sends them to
    # the wrong file
    with pytest.raises(DirectivesError, match=says):
        read_overrides(environ)


def test_the_command_line_is_read_as_the_alias_would_be() -> None:
    assert parse_override("epochs", "3") == 3
    assert parse_override("limit", "[0, 5]") == (0, 5)

    with pytest.raises(DirectivesError, match="epochs"):
        parse_override("epochs", "yes")
