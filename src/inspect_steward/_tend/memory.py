"""Host memory across a run: the series, the fit, and the sentences.

A fleet that outgrows its host is not an error anywhere Steward reads. The kernel kills a worker, the worker leaves no traceback, the task respawns into the same wall, and after `stall_after` attempts it is reported `stalled` — a diagnosis about progress, arrived at long after the memory that caused it was visible. What was visible was the host's available memory, falling turn over turn, and this module is the reading of it.

**The series is the journal's.** Every executing tend records the host's figures beside its observation, so the history is one walk over events the turn has already read, and a `status` — which records nothing — sees the same history plus the point it just took. Two consecutive `status` calls therefore show the same series with a fresh last point, which is the intended shape rather than a defect.

**Headroom, not available memory, is what the fit runs on.** Available RAM plus free swap is the quantity a fleet actually draws down, and it is the one the remedy moves: adding swap barely changes available memory and raises headroom by the whole of it, which is what lets the item clear and the ramp's gate reopen once somebody has acted.

**A straight line, on purpose.** The trend is ordinary least squares over the last two hours of points. The eval's own shape — how many samples, how long they run — is not knowable from outside it, and an agent reading this line does not need to know: some process on the machine is taking more memory every turn, and the line says when it runs out. Anything cleverer would be forecasting an eval Steward cannot see.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from .._util.duration import format_age
from .._util.jsonl import unix_time
from .._util.size import format_bytes
from .._worker import HostMemory
from .._workspace import OBSERVATION, JournalEvent

WINDOW = 2 * 3600.0
"""Seconds of history the fit looks back over.

Long enough for a slow leak to show as a slope rather than as noise between two tends, and short enough that a fleet whose shape changed an hour ago — tasks finishing, a step taken — is fitted on what it is doing now rather than on what it did overnight.
"""

MIN_POINTS = 3
"""Points before a line is drawn. Two points always make a line; three make a trend."""

LOW_FRACTION = 0.10
"""Headroom below this share of physical memory is *low*: the item fires and the ramp's window is not clean.

A share rather than a byte count, because what counts as a margin on a 16 GiB laptop is not a margin on a 512 GiB host — and the kernel's own reclaim starts squeezing well before zero, so a box at a tenth is already paying for it.
"""

SOON = 3600.0
"""Seconds ahead within which projected exhaustion is worth an item.

An hour is several tend intervals: enough turns for an agent to be scheduled, read the line, add swap, and see it hold — and short enough that a fleet whose growth is days from mattering is not raising an item every turn.
"""

FLAT = 64 * 1024**2 / 3600.0
"""Bytes per second under which a slope reads as *steady*. Sixty-four MiB an hour is well inside the jitter of a fleet's page cache."""


@dataclass(frozen=True)
class MemoryPoint:
    """One turn's reading of the host, as the journal recorded it."""

    ts: float
    """Unix seconds when the reading was taken."""

    total: int
    available: int
    swap_total: int
    swap_used: int

    rss: int
    """What the fleet's processes held resident at the time, in bytes."""

    @property
    def headroom(self) -> int:
        """Available memory plus free swap — see `HostMemory.headroom`."""
        return self.available + max(0, self.swap_total - self.swap_used)


@dataclass(frozen=True)
class Projection:
    """A straight line through the recent headroom readings."""

    slope: float
    """Bytes per second of headroom. Negative is falling."""

    span: float
    """Seconds between the first and last point fitted."""

    points: int
    """How many readings the line is over."""

    exhausted_in: float | None
    """Seconds from the latest reading until headroom reaches zero at this slope, or `None` where it is not falling."""


@dataclass(frozen=True)
class MemoryReport:
    """The host's memory this turn, and where it is heading.

    One object that the terminal, `status.md`, the item, and the ramp's gate all read, so that four surfaces cannot disagree about whether the machine is short of memory.
    """

    host: HostMemory
    """This turn's reading."""

    rss: int
    """What the fleet's processes hold resident, in bytes — the same figure the resources table sums."""

    projection: Projection | None
    """The trend, or `None` with fewer than `MIN_POINTS` readings behind this one."""

    since: str | None = None
    """When the host was last recorded with headroom to spare, or `None` where no tend has recorded one (`read_memory_since`).

    What makes a shortage an *episode*: the item's id carries it, so an acknowledgment covers this shortage and not the next one after a recovery. `None` on a report assembled by hand, which claims nothing about earlier turns.
    """

    @property
    def fraction(self) -> float:
        """Headroom as a share of physical memory. Zero on a host that reports no memory at all."""
        return self.host.headroom / self.host.total if self.host.total > 0 else 0.0

    @property
    def low(self) -> bool:
        """Whether headroom is under `LOW_FRACTION` of physical memory."""
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
        swap = (
            f"swap {format_bytes(host.swap_used)} of {format_bytes(host.swap_total)} used"
            if host.swap_total > 0
            else "no swap"
        )
        return " · ".join(
            [
                f"available {format_bytes(host.available)} of {format_bytes(host.total)}",
                swap,
                f"headroom {format_bytes(host.headroom)} ({self.fraction:.0%})",
                f"fleet {format_bytes(self.rss)} resident",
            ]
        )

    @property
    def trend(self) -> str | None:
        """The line's account of the recent readings, or `None` before there are enough."""
        projection = self.projection
        if projection is None:
            return None
        over = f"over {format_age(int(projection.span))} ({projection.points} points)"
        if abs(projection.slope) < FLAT:
            return f"headroom steady {over}"
        rate = f"{format_bytes(int(abs(projection.slope) * 3600))}/h"
        if projection.slope > 0:
            return f"headroom rising {rate} {over}"
        line = f"headroom falling {rate} {over}"
        if projection.exhausted_in is not None:
            line += f" · exhausted in ~{format_age(int(projection.exhausted_in))}"
        return line

    @property
    def lines(self) -> list[str]:
        """The figures, then the trend where there is one — one source for both renderings."""
        trend = self.trend
        return [self.figures] if trend is None else [self.figures, trend]


def read_memory_since(events: Sequence[JournalEvent]) -> str | None:
    """When a tend last recorded the host as not short, as the journal's timestamp.

    The boundary of the current shortage. Read off the recorded **tier** rather than off the item list, and the difference is what an acknowledgment does: an acknowledged item leaves the list while the host stays short, and reading that as a recovery would mint a new episode — and a new id — the turn after somebody accepted the old one. A turn with nothing running recorded no reading, which is not a shortage either.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        The newest such observation's timestamp, or `None` where every recorded observation was short, or none exist.
    """
    for event in reversed(events):
        if event.type != OBSERVATION:
            continue
        recorded = event.payload.get("memory")
        if not isinstance(recorded, dict):
            return event.ts
        if cast(dict[str, Any], recorded).get("tier") is None:
            return event.ts
    return None


def read_memory_history(
    events: Sequence[JournalEvent], *, now: float
) -> list[MemoryPoint]:
    """The recent host readings, oldest first.

    Walks back from the newest observation and **stops at the first one without a reading** — a turn with nothing running, or one recorded before the reading existed — so the series is the current running episode and a line is never fitted across a relaunch. Stops again past `WINDOW`, for the reason the constant gives.

    Args:
        events: Events in file order, as `read_journal` returns them.
        now: This turn's clock, unix, which the window is measured back from.

    Returns:
        Every reading inside the window and the episode, oldest first. Empty where the newest observation carried none.
    """
    points: list[MemoryPoint] = []
    for event in reversed(events):
        if event.type != OBSERVATION:
            continue
        recorded = event.payload.get("memory")
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


def project(series: Sequence[MemoryPoint]) -> Projection | None:
    """Fit a line through the headroom readings.

    Args:
        series: Readings in time order.

    Returns:
        The fit, or `None` with fewer than `MIN_POINTS` readings or no time between them.
    """
    if len(series) < MIN_POINTS:
        return None
    origin = series[0].ts
    xs = [point.ts - origin for point in series]
    ys = [float(point.headroom) for point in series]
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
    latest = series[-1].headroom
    exhausted_in = latest / -slope if slope < 0 else None
    return Projection(
        slope=slope, span=span, points=len(series), exhausted_in=exhausted_in
    )


def memory_report(
    host: HostMemory,
    rss: int,
    history: Sequence[MemoryPoint],
    *,
    now: float,
    since: str | None = None,
) -> MemoryReport:
    """This turn's report: the reading just taken, fitted with what the journal holds.

    Args:
        host: The reading.
        rss: The fleet's resident memory, summed once per process.
        history: Earlier readings, oldest first (`read_memory_history`).
        now: When the reading was taken, unix.
        since: When the host was last recorded as not short (`read_memory_since`).

    Returns:
        The report. The reading itself is the newest point of the fit, so a `status` sees the freshest trend without recording anything.
    """
    latest = MemoryPoint(
        ts=now,
        total=host.total,
        available=host.available,
        swap_total=host.swap_total,
        swap_used=host.swap_used,
        rss=rss,
    )
    return MemoryReport(
        host=host, rss=rss, projection=project([*history, latest]), since=since
    )


def memory_payload(report: MemoryReport | None) -> dict[str, Any] | None:
    """The `memory` an observation records, or `None` while nothing runs.

    `None` is written rather than omitted: it is what tells the next reader that the episode ended here, so a fleet launched tomorrow is not fitted against tonight's readings. The tier rides beside the figures so that `read_memory_since` can tell a turn that was not short from one whose item was merely acknowledged.

    Args:
        report: This turn's report.

    Returns:
        The payload for the observation's `memory` key.
    """
    if report is None:
        return None
    host = report.host
    return {
        "total": host.total,
        "available": host.available,
        "swap_total": host.swap_total,
        "swap_used": host.swap_used,
        "rss": report.rss,
        "tier": report.tier,
    }


def _point(ts: float, recorded: dict[str, Any]) -> MemoryPoint | None:
    """A reading from a payload, or `None` where a field is missing or is not an integer."""
    values: dict[str, int] = {}
    for key in ("total", "available", "swap_total", "swap_used", "rss"):
        value = recorded.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        values[key] = value
    return MemoryPoint(ts=ts, **values)
