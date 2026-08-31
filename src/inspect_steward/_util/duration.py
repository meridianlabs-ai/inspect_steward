"""Durations, as a human writes them and Steward stores them.

One interval reaches Steward from two directions — `_steward.yaml`'s `tend_interval` and a `--tend-interval` flag — and both are typed by a person. A bare number would make each of them ambiguous in the way that matters most: `tend_interval: 10` is ten seconds to whoever wrote the parser and ten minutes to whoever wrote the file, and the failure is silent in both directions. So the written form always carries its unit and the stored form is always seconds.
"""

import re
from datetime import datetime, timezone

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


def seconds_since(ts: str) -> float | None:
    """Seconds from a recorded instant until now, or `None` where it cannot be read.

    Unparseable rather than absent: a record written by a version that stamped its timestamps differently is history, not damage, and the caller's answer to *how long since* is then *unknown* rather than *forever*. A naive timestamp is unreadable for the same reason — subtracting it from UTC would be comparing two clocks nobody synchronized.

    Args:
        ts: A recorded instant, UTC ISO-8601 (`Z` or explicit offset).

    Returns:
        Seconds elapsed, or `None`.
    """
    try:
        recorded = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if recorded.tzinfo is None:
        return None
    return (datetime.now(timezone.utc) - recorded).total_seconds()


def is_after(instant: str, boundary: str) -> bool:
    """Whether one recorded instant is strictly after another, honestly false when either does not parse.

    The anomaly machinery's ordering primitive: an attempt against a ruling, a log against a spawn. Unlike `seconds_since`, a naive timestamp is read as UTC — the instants compared here come from eval headers, which carry an offset in practice, and a best-effort ordering of a legacy stamp beats refusing to order at all.

    Args:
        instant: The instant asked about, ISO-8601.
        boundary: The instant it must follow, ISO-8601.

    Returns:
        `True` only when both parse and `instant` is later.
    """
    left, right = _instant(instant), _instant(boundary)
    return left is not None and right is not None and left > right


def _instant(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


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
