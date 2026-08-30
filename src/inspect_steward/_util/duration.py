"""Durations, as a human writes them and Steward stores them.

One interval reaches Steward from two directions — `_steward.yaml`'s `tend_interval` and a `--interval` flag — and both are typed by a person. A bare number would make each of them ambiguous in the way that matters most: `tend_interval: 10` is ten seconds to whoever wrote the parser and ten minutes to whoever wrote the file, and the failure is silent in both directions. So the written form always carries its unit and the stored form is always seconds.
"""

import re

_DURATION = re.compile(r"^(\d+)\s*(s|m|h)$", re.IGNORECASE)

_SECONDS = {"s": 1, "m": 60, "h": 3600}

_UNITS = ("s", "m", "h")


class DurationError(ValueError):
    """A duration could not be read. Carries the text that arrived, since the whole point is to tell an author what they typed."""


def parse_duration(text: str) -> int:
    """Read a duration.

    Args:
        text: A count and a unit, e.g. `30s`, `10m`, `1h`. Whitespace between them is allowed; a bare number is not.

    Returns:
        Seconds.

    Raises:
        DurationError: Not a duration, or zero.
    """
    match = _DURATION.match(text.strip())
    if match is None:
        raise DurationError(
            f"'{text}' is not a duration — write a count and a unit, "
            f"one of {', '.join(_UNITS)} (e.g. '10m')"
        )
    seconds = int(match.group(1)) * _SECONDS[match.group(2).lower()]
    if seconds == 0:
        raise DurationError(f"'{text}' is zero, which is not an interval")
    return seconds


def format_duration(seconds: int) -> str:
    """Render seconds the way they would have been written.

    The inverse of `parse_duration` wherever the value divides evenly, which every value a person typed does. Used in messages rather than in storage, so an interval read back out of a journal is described the way its author wrote it.

    Args:
        seconds: A duration.

    Returns:
        The largest unit that divides it evenly, e.g. `600` → `10m`.
    """
    for unit in reversed(_UNITS):
        size = _SECONDS[unit]
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
