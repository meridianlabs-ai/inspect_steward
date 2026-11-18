"""Host disk across a run: the series, the fit, and the sentences.

A fleet writing `.eval` logs draws down the filesystem they land on, and when that filesystem fills the write fails, the worker dies with no clean traceback, and the run degrades with nothing pointing at the cause until long after the space that caused it was visible. What was visible was the free space on the log directory's filesystem, falling turn over turn, and this module is the reading of it.

It is the disk twin of `memory.py`, kept beside it rather than folded into it: the two answer the same shape of question about different resources and will diverge as each grows its own remedies. Disk has no swap analogue — free space is free space — so `headroom` is simply `free`; and there is no fleet-`rss` analogue, because the quantity that matters is what the host has left, not which process is writing.

**The series is the journal's.** Every executing tend records the filesystem's figures beside its observation, so the history is one walk over events the turn has already read, and a `status` — which records nothing — sees the same history plus the point it just took.

**A straight line, on purpose.** The trend is ordinary least squares over the last two hours of points. How fast a fleet writes is not knowable from outside it, and an agent reading this line does not need to know: free space is falling at some rate, and the line says when it runs out.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from .._util.duration import format_age
from .._util.jsonl import unix_time
from .._util.size import format_bytes
from .._worker import HostDisk
from .._workspace import OBSERVATION, JournalEvent

WINDOW = 2 * 3600.0
"""Seconds of history the fit looks back over.

Long enough for a slow fill to show as a slope rather than as noise between two tends, and short enough that a fleet whose shape changed an hour ago — tasks finishing, logs rotated — is fitted on what it is writing now rather than on what it wrote overnight.
"""

MIN_POINTS = 3
"""Points before a line is drawn. Two points always make a line; three make a trend."""

LOW_FRACTION = 0.05
"""Free space below this share of the filesystem is *low*: the item fires.

A share rather than a byte count, because what counts as a margin on a 100 GiB volume is not a margin on a 4 TiB one. A twentieth rather than memory's tenth: a disk takes no reclaim pressure on the way down — it is fine until the write that fails, which is nearer the floor than the kernel's squeeze on memory — so the mark sits lower before it is worth an item.
"""

SOON = 3600.0
"""Seconds ahead within which projected exhaustion is worth an item.

An hour is several tend intervals: enough turns for an agent to be scheduled, read the line, free space, and see it hold — and short enough that a fleet whose fill is days from mattering is not raising an item every turn.
"""

FLAT = 64 * 1024**2 / 3600.0
"""Bytes per second under which a slope reads as *steady*. Sixty-four MiB an hour is well inside the jitter of a filesystem a fleet and the rest of the box both write."""


@dataclass(frozen=True)
class DiskPoint:
    """One turn's reading of the filesystem, as the journal recorded it."""

    ts: float
    """Unix seconds when the reading was taken."""

    total: int
    free: int

    @property
    def headroom(self) -> int:
        """Free space — see `HostDisk.headroom`."""
        return self.free


@dataclass(frozen=True)
class Projection:
    """A straight line through the recent free-space readings."""

    slope: float
    """Bytes per second of free space. Negative is falling."""

    span: float
    """Seconds between the first and last point fitted."""

    points: int
    """How many readings the line is over."""

    exhausted_in: float | None
    """Seconds from the latest reading until free space reaches zero at this slope, or `None` where it is not falling."""


@dataclass(frozen=True)
class DiskReport:
    """The filesystem this turn, and where it is heading.

    One object that the terminal, `status.md`, and the item all read, so that the surfaces cannot disagree about whether the disk is short.
    """

    host: HostDisk
    """This turn's reading."""

    projection: Projection | None
    """The trend, or `None` with fewer than `MIN_POINTS` readings behind this one."""

    since: str | None = None
    """When the filesystem was last recorded with space to spare, or `None` where no tend has recorded one (`read_disk_since`).

    What makes a shortage an *episode*: the item's id carries it, so an acknowledgment covers this shortage and not the next one after a recovery. `None` on a report assembled by hand, which claims nothing about earlier turns.
    """

    @property
    def fraction(self) -> float:
        """Free space as a share of the filesystem. Zero on one that reports no size at all."""
        return self.host.free / self.host.total if self.host.total > 0 else 0.0

    @property
    def low(self) -> bool:
        """Whether free space is under `LOW_FRACTION` of the filesystem."""
        return self.host.total > 0 and self.fraction < LOW_FRACTION

    @property
    def tier(self) -> Literal["low", "projected"] | None:
        """Which condition holds, `low` winning: a fact about now outranks a forecast."""
        if self.low:
            return "low"
        projection = self.projection
        if (
            projection is not None
            and projection.exhausted_in is not None
            and projection.exhausted_in < SOON
        ):
            return "projected"
        return None

    @property
    def figures(self) -> str:
        """The reading as one line, which every rendering puts its own label in front of."""
        host = self.host
        return f"free {format_bytes(host.free)} of {format_bytes(host.total)} ({self.fraction:.0%})"

    @property
    def trend(self) -> str | None:
        """The line's account of the recent readings, or `None` before there are enough."""
        projection = self.projection
        if projection is None:
            return None
        over = f"over {format_age(int(projection.span))} ({projection.points} points)"
        if abs(projection.slope) < FLAT:
            return f"free steady {over}"
        rate = f"{format_bytes(int(abs(projection.slope) * 3600))}/h"
        if projection.slope > 0:
            return f"free rising {rate} {over}"
        line = f"free falling {rate} {over}"
        if projection.exhausted_in is not None:
            line += f" · full in ~{format_age(int(projection.exhausted_in))}"
        return line

    @property
    def lines(self) -> list[str]:
        """The figures, then the trend where there is one — one source for both renderings."""
        trend = self.trend
        return [self.figures] if trend is None else [self.figures, trend]


def read_disk_since(events: Sequence[JournalEvent]) -> str | None:
    """When a tend last recorded the filesystem as not short, as the journal's timestamp.

    The boundary of the current shortage. Read off the recorded **tier** rather than off the item list, and the difference is what an acknowledgment does: an acknowledged item leaves the list while the disk stays short, and reading that as a recovery would mint a new episode — and a new id — the turn after somebody accepted the old one. A turn with nothing running recorded no reading, which is not a shortage either.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The newest such observation's timestamp, or `None` where every recorded observation was short, or none exist.
    """
    for event in reversed(events):
        if event.type != OBSERVATION:
            continue
        recorded = event.payload.get("disk")
        if not isinstance(recorded, dict):
            return event.ts
        if cast(dict[str, Any], recorded).get("tier") is None:
            return event.ts
    return None


def read_disk_history(events: Sequence[JournalEvent], *, now: float) -> list[DiskPoint]:
    """The recent filesystem readings, oldest first.

    Walks back from the newest observation and **stops at the first one without a reading** — a turn with nothing running, or one recorded before the reading existed — so the series is the current running episode and a line is never fitted across a relaunch. Stops again past `WINDOW`, for the reason the constant gives.

    Args:
        events: Events in file order, as `read_journal` returns them.
        now: This turn's clock, unix, which the window is measured back from.

    Returns:
        Every reading inside the window and the episode, oldest first. Empty where the newest observation carried none.
    """
    points: list[DiskPoint] = []
    for event in reversed(events):
        if event.type != OBSERVATION:
            continue
        recorded = event.payload.get("disk")
        if not isinstance(recorded, dict):
            break
        ts = unix_time(event.ts)
        if ts is None:
            continue
        if now - ts > WINDOW:
            break
        point = _point(ts, cast(dict[str, Any], recorded))
        if point is not None:
            points.append(point)
    points.reverse()
    return points


def project(series: Sequence[DiskPoint]) -> Projection | None:
    """Fit a line through the free-space readings.

    Args:
        series: Readings in time order.

    Returns:
        The fit, or `None` with fewer than `MIN_POINTS` readings or no time between them.
    """
    if len(series) < MIN_POINTS:
        return None
    origin = series[0].ts
    xs = [point.ts - origin for point in series]
    ys = [float(point.free) for point in series]
    span = xs[-1] - xs[0]
    if span <= 0:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        return None
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = covariance / variance
    latest = series[-1].free
    exhausted_in = latest / -slope if slope < 0 else None
    return Projection(
        slope=slope, span=span, points=len(series), exhausted_in=exhausted_in
    )


def disk_report(
    host: HostDisk,
    history: Sequence[DiskPoint],
    *,
    now: float,
    since: str | None = None,
) -> DiskReport:
    """This turn's report: the reading just taken, fitted with what the journal holds.

    Args:
        host: The reading.
        history: Earlier readings, oldest first (`read_disk_history`).
        now: When the reading was taken, unix.
        since: When the filesystem was last recorded as not short (`read_disk_since`).

    Returns:
        The report. The reading itself is the newest point of the fit, so a `status` sees the freshest trend without recording anything.
    """
    latest = DiskPoint(ts=now, total=host.total, free=host.free)
    return DiskReport(host=host, projection=project([*history, latest]), since=since)


def disk_payload(report: DiskReport | None) -> dict[str, Any] | None:
    """The `disk` an observation records, or `None` while nothing runs.

    `None` is written rather than omitted: it is what tells the next reader that the episode ended here, so a fleet launched tomorrow is not fitted against tonight's readings. The tier rides beside the figures so that `read_disk_since` can tell a turn that was not short from one whose item was merely acknowledged.

    Args:
        report: This turn's report.

    Returns:
        The payload for the observation's `disk` key.
    """
    if report is None:
        return None
    host = report.host
    return {
        "total": host.total,
        "free": host.free,
        "tier": report.tier,
    }


def _point(ts: float, recorded: dict[str, Any]) -> DiskPoint | None:
    """A reading from a payload, or `None` where a field is missing or is not an integer."""
    values: dict[str, int] = {}
    for key in ("total", "free"):
        value = recorded.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        values[key] = value
    return DiskPoint(ts=ts, **values)
