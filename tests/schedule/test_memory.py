"""Host memory across a run: the series the journal holds, the line through it, and what the line says.

The fit is pure and driven from synthetic series, because the one input a test
cannot manufacture is a host actually running out of memory. What is checked is
the shape of the answer: a decline projects a time, a flat line projects none,
too few points project nothing, and the words the report chooses follow the
tier the numbers put it in.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from inspect_steward._tend.memory import (
    LOW_FRACTION,
    MIN_POINTS,
    SOON,
    WINDOW,
    MemoryPoint,
    MemoryReport,
    Projection,
    memory_payload,
    memory_report,
    project,
    read_memory_history,
    read_memory_since,
)
from inspect_steward._worker import HostMemory
from inspect_steward._workspace import OBSERVATION, read_journal

GIB = 1024**3
TOTAL = 64 * GIB


def point(ts: float, headroom: int, *, swap_total: int = 0) -> MemoryPoint:
    """A reading with the given headroom, all of it in available memory."""
    return MemoryPoint(
        ts=ts,
        total=TOTAL,
        available=headroom,
        swap_total=swap_total,
        swap_used=0,
        rss=10 * GIB,
    )


def host(available: int, *, swap_total: int = 0, swap_used: int = 0) -> HostMemory:
    return HostMemory(
        total=TOTAL, available=available, swap_total=swap_total, swap_used=swap_used
    )


# --- the fit ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("series", "slope", "exhausted_in"),
    [
        pytest.param(
            [point(0, 30 * GIB), point(600, 29 * GIB), point(1200, 28 * GIB)],
            -GIB / 600,
            28 * GIB / (GIB / 600),
            id="a straight decline projects the exact time to zero",
        ),
        pytest.param(
            [point(0, 30 * GIB), point(600, 30 * GIB), point(1200, 30 * GIB)],
            0.0,
            None,
            id="a flat line projects no exhaustion",
        ),
        pytest.param(
            [point(0, 28 * GIB), point(600, 29 * GIB), point(1200, 30 * GIB)],
            GIB / 600,
            None,
            id="a rising line projects no exhaustion",
        ),
    ],
)
def test_the_line_through_the_readings(
    series: list[MemoryPoint], slope: float, exhausted_in: float | None
) -> None:
    fit = project(series)

    assert fit is not None
    assert fit.slope == pytest.approx(slope)
    assert fit.points == len(series)
    assert fit.span == series[-1].ts - series[0].ts
    if exhausted_in is None:
        assert fit.exhausted_in is None
    else:
        assert fit.exhausted_in == pytest.approx(exhausted_in)


def test_a_noisy_decline_still_falls() -> None:
    # jitter of a gibibyte either side of a steady loss: the sign and the rough
    # time are what an agent reads, not the third decimal
    series = [
        point(0, 40 * GIB),
        point(600, 40 * GIB),
        point(1200, 37 * GIB),
        point(1800, 38 * GIB),
        point(2400, 35 * GIB),
    ]

    fit = project(series)

    assert fit is not None
    assert fit.slope < 0
    assert fit.exhausted_in is not None
    assert 3 * 3600 < fit.exhausted_in < 6 * 3600


@pytest.mark.parametrize(
    "series",
    [
        pytest.param([point(0, 30 * GIB), point(600, 29 * GIB)], id="two points"),
        pytest.param([], id="no points"),
        pytest.param(
            [point(0, 30 * GIB)] * MIN_POINTS, id="enough points with no time between"
        ),
    ],
)
def test_too_little_to_fit_is_no_line(series: list[MemoryPoint]) -> None:
    assert project(series) is None


# --- the series the journal holds -------------------------------------------


def observe(
    journal: Path, ts: str, memory: dict[str, int | str | None] | None | str
) -> None:
    """An observation with a chosen timestamp, which `append_event` will not give."""
    with journal.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"ts": ts, "type": OBSERVATION, "memory": memory}) + "\n")


def reading(available: int, tier: str | None = None) -> dict[str, int | str | None]:
    return {
        "total": TOTAL,
        "available": available,
        "swap_total": 0,
        "swap_used": 0,
        "rss": 10 * GIB,
        "tier": tier,
    }


NOW = 1_800_000_000.0
"""2027-01-15T08:00:00Z, the clock every history read below measures back from."""


def at(seconds_ago: float) -> str:
    return (
        datetime.fromtimestamp(NOW - seconds_ago, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def test_the_history_is_read_oldest_first(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(1800), reading(30 * GIB))
    observe(journal, at(1200), reading(29 * GIB))
    observe(journal, at(600), reading(28 * GIB))

    history = read_memory_history(read_journal(journal).events, now=NOW)

    assert [entry.available for entry in history] == [30 * GIB, 29 * GIB, 28 * GIB]
    assert history[0].ts == pytest.approx(NOW - 1800)
    assert history[0].rss == 10 * GIB


def test_a_turn_with_nothing_running_ends_the_series(tmp_path: Path) -> None:
    # last night's fleet, an idle turn, then tonight's: a line through both
    # would be fitted against a box that was doing something else
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(1800), reading(10 * GIB))
    observe(journal, at(1200), None)
    observe(journal, at(600), reading(30 * GIB))

    history = read_memory_history(read_journal(journal).events, now=NOW)

    assert [entry.available for entry in history] == [30 * GIB]


def test_a_reading_older_than_the_window_is_left_out(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(WINDOW + 600), reading(40 * GIB))
    observe(journal, at(600), reading(30 * GIB))

    history = read_memory_history(read_journal(journal).events, now=NOW)

    assert [entry.available for entry in history] == [30 * GIB]


def test_a_malformed_reading_is_skipped_not_fatal(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(1200), reading(30 * GIB))
    observe(journal, at(600), {**reading(29 * GIB), "available": True})

    history = read_memory_history(read_journal(journal).events, now=NOW)

    assert [entry.available for entry in history] == [30 * GIB]


def test_the_payload_reads_back_as_the_point_it_recorded(tmp_path: Path) -> None:
    # the round trip the turn relies on: what `_record` writes is what the next
    # turn's fit is over
    report = memory_report(
        host(12 * GIB, swap_total=8 * GIB, swap_used=2 * GIB), 40 * GIB, [], now=NOW
    )
    payload = memory_payload(report)
    assert payload is not None
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(600), payload)

    (entry,) = read_memory_history(read_journal(journal).events, now=NOW)

    assert (entry.available, entry.swap_total, entry.swap_used, entry.rss) == (
        12 * GIB,
        8 * GIB,
        2 * GIB,
        40 * GIB,
    )
    assert entry.headroom == 18 * GIB
    assert memory_payload(None) is None


def test_the_episode_boundary_is_the_last_turn_that_was_not_short(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(2400), reading(30 * GIB))
    observe(journal, at(1800), reading(4 * GIB, "low"))
    # an acknowledged shortage leaves the item list and not the tier, so this
    # turn is still short and does not move the boundary
    observe(journal, at(1200), reading(4 * GIB, "low"))
    observe(journal, at(600), reading(12 * GIB, "projected"))

    since = read_memory_since(read_journal(journal).events)

    assert since == at(2400)


def test_a_turn_with_nothing_running_is_not_short(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(1200), reading(4 * GIB, "low"))
    observe(journal, at(600), None)

    assert read_memory_since(read_journal(journal).events) == at(600)


def test_a_workspace_short_since_its_first_tend_has_no_boundary(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(600), reading(4 * GIB, "low"))

    assert read_memory_since(read_journal(journal).events) is None
    assert read_memory_since([]) is None


def test_the_payload_carries_the_tier_and_the_report_its_episode() -> None:
    report = memory_report(
        host(4 * GIB), 40 * GIB, [], now=NOW, since="2026-09-19T01:00:00Z"
    )
    payload = memory_payload(report)

    assert payload is not None and payload["tier"] == "low"
    assert report.since == "2026-09-19T01:00:00Z"
    healthy = memory_payload(memory_report(host(30 * GIB), 40 * GIB, [], now=NOW))
    assert healthy is not None and healthy["tier"] is None


def test_the_report_fits_the_reading_just_taken_with_the_history() -> None:
    # two recorded points and this turn's reading make three, which is a line;
    # a status therefore sees the freshest trend without recording anything
    history = [point(NOW - 1200, 30 * GIB), point(NOW - 600, 29 * GIB)]

    report = memory_report(host(28 * GIB), 10 * GIB, history, now=NOW)

    assert report.projection is not None
    assert report.projection.points == 3
    assert report.projection.slope == pytest.approx(-GIB / 600)


# --- what the report says ---------------------------------------------------


def falling(exhausted_in: float) -> Projection:
    return Projection(slope=-GIB / 3600, span=3600, points=7, exhausted_in=exhausted_in)


@pytest.mark.parametrize(
    ("report", "tier", "said"),
    [
        pytest.param(
            MemoryReport(host=host(4 * GIB), rss=50 * GIB, projection=None),
            "low",
            ["headroom 4.0 GiB (6%)", "no swap"],
            id="under a tenth of RAM is low",
        ),
        pytest.param(
            MemoryReport(
                host=host(4 * GIB, swap_total=16 * GIB, swap_used=2 * GIB),
                rss=50 * GIB,
                projection=None,
            ),
            None,
            ["swap 2.0 GiB of 16.0 GiB used", "headroom 18.0 GiB (28%)"],
            id="free swap lifts the same host out of low",
        ),
        pytest.param(
            MemoryReport(
                host=host(20 * GIB), rss=40 * GIB, projection=falling(SOON / 2)
            ),
            "projected",
            ["headroom falling 1.0 GiB/h over 1h (7 points)", "exhausted in ~30m"],
            id="exhaustion inside the horizon is projected",
        ),
        pytest.param(
            MemoryReport(
                host=host(20 * GIB), rss=40 * GIB, projection=falling(SOON * 3)
            ),
            None,
            ["exhausted in ~3h"],
            id="exhaustion past the horizon is reported and not raised",
        ),
        pytest.param(
            MemoryReport(
                host=host(4 * GIB), rss=50 * GIB, projection=falling(SOON / 2)
            ),
            "low",
            [],
            id="low wins over projected",
        ),
        pytest.param(
            MemoryReport(
                host=host(20 * GIB),
                rss=40 * GIB,
                projection=Projection(
                    slope=-1.0, span=3600, points=7, exhausted_in=None
                ),
            ),
            None,
            ["headroom steady over 1h (7 points)"],
            id="a slope inside the jitter reads as steady",
        ),
        pytest.param(
            MemoryReport(
                host=host(20 * GIB),
                rss=40 * GIB,
                projection=Projection(
                    slope=GIB / 3600, span=3600, points=7, exhausted_in=None
                ),
            ),
            None,
            ["headroom rising 1.0 GiB/h"],
            id="a rising line says so",
        ),
        pytest.param(
            MemoryReport(
                host=HostMemory(total=0, available=0, swap_total=0, swap_used=0),
                rss=0,
                projection=None,
            ),
            None,
            [],
            id="a host reporting no memory at all is not low",
        ),
    ],
)
def test_the_tier_and_the_sentences(
    report: MemoryReport, tier: str | None, said: list[str]
) -> None:
    assert report.tier == tier
    joined = " ".join(report.lines)
    for phrase in said:
        assert phrase in joined
    assert report.lines[0].startswith("available ")
    assert len(report.lines) == (1 if report.projection is None else 2)


def test_the_low_mark_is_a_share_of_physical_memory() -> None:
    just_under = host(int(TOTAL * LOW_FRACTION) - 1)
    just_over = host(int(TOTAL * LOW_FRACTION) + 1)

    assert MemoryReport(host=just_under, rss=0, projection=None).low
    assert not MemoryReport(host=just_over, rss=0, projection=None).low
