"""Inspect's words reaching a Steward run.

Two claims, and the second is the one that would rot quietly: every overridable `eval_set()` argument has a spelling here, and the narrower spelling wins. The rest is the shared-parser property one vocabulary over — a value refused in `STEWARD_EPOCHS` is refused identically in `INSPECT_EVAL_EPOCHS` and on `steward launch --epochs`.
"""

from typing import Any

import pytest
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from inspect_steward._workspace import (
    ALIASES,
    LOG_DIR,
    VARIABLES,
    DirectivesError,
    parse_override,
    read_overrides,
)


def test_every_overridable_argument_has_a_spelling() -> None:
    # the map is data rather than a rule -- log_shared answers to two variables,
    # log_level drops the EVAL_ infix, four fields are only spelled negatively --
    # so a field added upstream would otherwise arrive silently unmapped
    assert set(VARIABLES) == set(EvalSetOverrides.model_fields)


def test_the_log_directory_is_stewards_alone() -> None:
    # the run's logs go where the fleet is watched from, so there is no scope at
    # which somebody else decides -- no alias, and no inspect spelling read here
    assert VARIABLES["log_dir"] == ((), None)
    assert f"STEWARD_{LOG_DIR}" not in ALIASES
    assert "STEWARD_LOG_DIR" not in ALIASES


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
        "a second variable for one field",
        {"INSPECT_LOG_SHARED": "30"},
        {},
        "log_shared",
        30,
    ),
    (
        "the more specific of the two",
        {"INSPECT_LOG_SHARED": "30", "INSPECT_EVAL_LOG_SHARED": "10"},
        {},
        "log_shared",
        30,
    ),
    (
        "a field inspect only spells negatively",
        {"INSPECT_EVAL_NO_SCORE": "1"},
        {},
        "score",
        False,
    ),
    ("a range", {"STEWARD_LIMIT": "[0, 5]"}, {}, "limit", (0, 5)),
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


def test_silence_is_no_document_at_all() -> None:
    # an empty overrides object and no object are the same instruction -- keep
    # what the definition chose -- and only one of them is worth writing down
    assert read_overrides({}) is None
    assert read_overrides({"PATH": "/usr/bin"}, {"epochs": None}) is None


REFUSED: list[tuple[str, dict[str, str], str]] = [
    ("a coerced count", {"STEWARD_EPOCHS": "yes"}, "STEWARD_EPOCHS"),
    ("a quoted count", {"INSPECT_EVAL_MAX_SAMPLES": "'8'"}, "INSPECT_EVAL_MAX_SAMPLES"),
    ("a meaningless range", {"STEWARD_LIMIT": "[5, 5]"}, "STEWARD_LIMIT"),
    (
        "an identity-bearing generate config",
        {"STEWARD_GENERATE_CONFIG": "{temperature: 0.5}"},
        "temperature",
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


def test_the_command_line_is_read_as_the_variable_would_be() -> None:
    assert parse_override("epochs", "3") == 3
    assert parse_override("limit", "[0, 5]") == (0, 5)

    with pytest.raises(DirectivesError, match="epochs"):
        parse_override("epochs", "yes")
