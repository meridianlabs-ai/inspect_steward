"""Host disk across a run: the series the journal holds, the line through it, and what the line says.

The fit is pure and driven from synthetic series, because the one input a test
cannot manufacture is a filesystem actually filling under a running fleet. What
is checked is the shape of the answer: a decline projects a time, a flat line
projects none, too few points project nothing, and the words the report chooses
follow the tier the numbers put it in.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from inspect_steward._tend.disk import (
    LOW_FRACTION,
    MIN_POINTS,
    SOON,
    WINDOW,
    DiskPoint,
    DiskReport,
    Projection,
    disk_payload,
    disk_report,
    project,
    read_disk_history,
    read_disk_since,
)
from inspect_steward._worker import HostDisk
from inspect_steward._workspace import OBSERVATION, read_journal

GIB = 1024**3
TOTAL = 500 * GIB


def point(ts: float, free: int) -> DiskPoint:
    """A reading with the given free space."""
    return DiskPoint(ts=ts, total=TOTAL, free=free)


def host(free: int) -> HostDisk:
    return HostDisk(total=TOTAL, free=free)


# --- the fit ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("series", "slope", "exhausted_in"),
    [
        pytest.param(
            [point(0, 30 * GIB), point(600, 29 * GIB), point(1200, 28 * GIB)],
            -GIB / 600,
            28 * GIB / (GIB / 600),
            id="a straight decline projects the exact time to full",
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
    series: list[DiskPoint], slope: float, exhausted_in: float | None
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
def test_too_little_to_fit_is_no_line(series: list[DiskPoint]) -> None:
    assert project(series) is None


# --- the series the journal holds -------------------------------------------


def observe(
    journal: Path, ts: str, disk: dict[str, int | str | None] | None | str
) -> None:
    """An observation with a chosen timestamp, which `append_event` will not give."""
    with journal.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"ts": ts, "type": OBSERVATION, "disk": disk}) + "\n")


def reading(free: int, tier: str | None = None) -> dict[str, int | str | None]:
    return {"total": TOTAL, "free": free, "tier": tier}


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

    history = read_disk_history(read_journal(journal).events, now=NOW)

    assert [entry.free for entry in history] == [30 * GIB, 29 * GIB, 28 * GIB]
    assert history[0].ts == pytest.approx(NOW - 1800)


def test_a_turn_with_nothing_running_ends_the_series(tmp_path: Path) -> None:
    # last night's fleet, an idle turn, then tonight's: a line through both
    # would be fitted against a box that was writing something else
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(1800), reading(10 * GIB))
    observe(journal, at(1200), None)
    observe(journal, at(600), reading(30 * GIB))

    history = read_disk_history(read_journal(journal).events, now=NOW)

    assert [entry.free for entry in history] == [30 * GIB]


def test_a_reading_older_than_the_window_is_left_out(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(WINDOW + 600), reading(40 * GIB))
    observe(journal, at(600), reading(30 * GIB))

    history = read_disk_history(read_journal(journal).events, now=NOW)

    assert [entry.free for entry in history] == [30 * GIB]


def test_a_malformed_reading_is_skipped_not_fatal(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(1200), reading(30 * GIB))
    observe(journal, at(600), {**reading(29 * GIB), "free": True})

    history = read_disk_history(read_journal(journal).events, now=NOW)

    assert [entry.free for entry in history] == [30 * GIB]


def test_the_payload_reads_back_as_the_point_it_recorded(tmp_path: Path) -> None:
    # the round trip the turn relies on: what the observation writes is what the
    # next turn's fit is over
    report = disk_report(host(44 * GIB), [], now=NOW)
    payload = disk_payload(report)
    assert payload is not None
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(600), payload)

    (entry,) = read_disk_history(read_journal(journal).events, now=NOW)

    assert (entry.total, entry.free) == (TOTAL, 44 * GIB)
    assert entry.headroom == 44 * GIB
    assert disk_payload(None) is None


def test_the_episode_boundary_is_the_last_turn_that_was_not_short(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(2400), reading(100 * GIB))
    observe(journal, at(1800), reading(4 * GIB, "low"))
    # an acknowledged shortage leaves the item list and not the tier, so this
    # turn is still short and does not move the boundary
    observe(journal, at(1200), reading(4 * GIB, "low"))
    observe(journal, at(600), reading(12 * GIB, "projected"))

    since = read_disk_since(read_journal(journal).events)

    assert since == at(2400)


def test_a_turn_with_nothing_running_is_not_short(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(1200), reading(4 * GIB, "low"))
    observe(journal, at(600), None)

    assert read_disk_since(read_journal(journal).events) == at(600)


def test_a_workspace_short_since_its_first_tend_has_no_boundary(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    observe(journal, at(600), reading(4 * GIB, "low"))

    assert read_disk_since(read_journal(journal).events) is None
    assert read_disk_since([]) is None


def test_the_payload_carries_the_tier_and_the_report_its_episode() -> None:
    report = disk_report(host(4 * GIB), [], now=NOW, since="2026-09-19T01:00:00Z")
    payload = disk_payload(report)

    assert payload is not None and payload["tier"] == "low"
    assert report.since == "2026-09-19T01:00:00Z"
    healthy = disk_payload(disk_report(host(300 * GIB), [], now=NOW))
    assert healthy is not None and healthy["tier"] is None


def test_the_report_fits_the_reading_just_taken_with_the_history() -> None:
    # two recorded points and this turn's reading make three, which is a line;
    # a status therefore sees the freshest trend without recording anything
    history = [point(NOW - 1200, 30 * GIB), point(NOW - 600, 29 * GIB)]

    report = disk_report(host(28 * GIB), history, now=NOW)

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
            DiskReport(host=host(4 * GIB), projection=None),
            "low",
            ["free 4.0 GiB of 500.0 GiB (1%)"],
            id="under a twentieth of the filesystem is low",
        ),
        pytest.param(
            DiskReport(host=host(100 * GIB), projection=None),
            None,
            ["free 100.0 GiB of 500.0 GiB (20%)"],
            id="a fifth free is not low",
        ),
        pytest.param(
            DiskReport(host=host(200 * GIB), projection=falling(SOON / 2)),
            "projected",
            ["free falling 1.0 GiB/h over 1h (7 points)", "full in ~30m"],
            id="exhaustion inside the horizon is projected",
        ),
        pytest.param(
            DiskReport(host=host(200 * GIB), projection=falling(SOON * 3)),
            None,
            ["full in ~3h"],
            id="exhaustion past the horizon is reported and not raised",
        ),
        pytest.param(
            DiskReport(host=host(4 * GIB), projection=falling(SOON / 2)),
            "low",
            [],
            id="low wins over projected",
        ),
        pytest.param(
            DiskReport(
                host=host(200 * GIB),
                projection=Projection(
                    slope=-1.0, span=3600, points=7, exhausted_in=None
                ),
            ),
            None,
            ["free steady over 1h (7 points)"],
            id="a slope inside the jitter reads as steady",
        ),
        pytest.param(
            DiskReport(
                host=host(200 * GIB),
                projection=Projection(
                    slope=GIB / 3600, span=3600, points=7, exhausted_in=None
                ),
            ),
            None,
            ["free rising 1.0 GiB/h"],
            id="a rising line says so",
        ),
        pytest.param(
            DiskReport(host=HostDisk(total=0, free=0), projection=None),
            None,
            [],
            id="a filesystem reporting no size at all is not low",
        ),
    ],
)
def test_the_tier_and_the_sentences(
    report: DiskReport, tier: str | None, said: list[str]
) -> None:
    assert report.tier == tier
    joined = " ".join(report.lines)
    for phrase in said:
        assert phrase in joined
    assert report.lines[0].startswith("free ")
    assert len(report.lines) == (1 if report.projection is None else 2)


def test_the_low_mark_is_a_share_of_the_filesystem() -> None:
    just_under = host(int(TOTAL * LOW_FRACTION) - 1)
    just_over = host(int(TOTAL * LOW_FRACTION) + 1)

    assert DiskReport(host=just_under, projection=None).low
    assert not DiskReport(host=just_over, projection=None).low
