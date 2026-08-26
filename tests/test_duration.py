"""Durations, as a human writes them.

Small, and here because one interval reaches Steward from two directions —
`_steward.md`'s front matter and a `--interval` flag — and both are typed by a
person. What earns the tests is the refusal: a bare number is the one input
where being helpful would be being wrong, since `10` is ten minutes to whoever
wrote it and ten seconds to whoever wrote the parser.
"""

import pytest
from inspect_steward._util.duration import (
    DurationError,
    format_duration,
    parse_duration,
)

GOOD: list[tuple[str, str, int]] = [
    ("seconds", "30s", 30),
    ("minutes", "10m", 600),
    ("hours", "2h", 7200),
    ("uppercase", "10M", 600),
    ("a space between", "10 m", 600),
    ("surrounding space", "  10m  ", 600),
    ("one", "1s", 1),
]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [(text, seconds) for _, text, seconds in GOOD],
    ids=[case for case, _, _ in GOOD],
)
def test_a_duration_is_a_count_and_a_unit(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


BAD: list[tuple[str, str]] = [
    ("a bare number", "10"),
    ("a unit Steward does not use", "10d"),
    ("a fraction", "1.5m"),
    ("nothing", ""),
    ("only a unit", "m"),
    ("words", "ten minutes"),
    ("negative", "-10m"),
    ("zero", "0m"),
]


@pytest.mark.parametrize(
    "text", [text for _, text in BAD], ids=[case for case, _ in BAD]
)
def test_anything_else_names_what_arrived(text: str) -> None:
    # the author has to see what they typed, because the whole class of mistake
    # here is one character
    with pytest.raises(DurationError) as raised:
        parse_duration(text)

    assert f"'{text}'" in str(raised.value)


ROUND: list[tuple[str, int, str]] = [
    ("minutes", 600, "10m"),
    ("hours", 7200, "2h"),
    ("seconds", 30, "30s"),
    ("not a whole minute", 90, "90s"),
    ("not a whole hour", 5400, "90m"),
]


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(seconds, text) for _, seconds, text in ROUND],
    ids=[case for case, _, _ in ROUND],
)
def test_seconds_are_rendered_the_way_they_were_written(
    seconds: int, text: str
) -> None:
    # messages say `10m` because that is what the author typed; the largest
    # unit that divides evenly recovers it for every value a person writes
    assert format_duration(seconds) == text
    assert parse_duration(text) == seconds
