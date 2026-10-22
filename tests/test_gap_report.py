"""Tests for the gap report."""

from datetime import UTC, datetime, timedelta

import pandas as pd
from helpers import START, make_rows

from flowguard.features.gap_report import format_report, gap_report
from flowguard.features.hourly_grid import build_hourly_grid

BAFU_HOLE = datetime(2026, 10, 8, 1, tzinfo=UTC)


def report_for(rows, **kwargs):
    result = build_hourly_grid(rows, **kwargs)
    return gap_report(result.grid, result.duplicates)


def test_gaps_and_summary():
    # Hours 0..9 with 2-3 and 6 missing: two gaps, longest 2 h, 3 of 10 missing.
    report = report_for(make_rows([0, 1, 4, 5, 7, 8, 9]))
    gaps = report.gaps
    assert list(gaps["hours"]) == [2, 1]
    assert gaps.iloc[0]["start"] == pd.Timestamp(START + timedelta(hours=2))
    assert gaps.iloc[0]["end"] == pd.Timestamp(START + timedelta(hours=3))
    s = report.summary.iloc[0]
    assert (s["n_gaps"], s["longest_gap_hours"], s["missing_hours"], s["hours_total"]) == (
        2,
        2,
        3,
        10,
    )
    assert s["share_missing"] == 0.3


def test_no_gap_gives_empty_gaps_and_zero_summary():
    report = report_for(make_rows(range(4)))
    assert report.gaps.empty
    s = report.summary.iloc[0]
    assert (s["n_gaps"], s["longest_gap_hours"], s["share_missing"]) == (0, 0, 0)


def test_duplicates_are_passed_on():
    report = report_for(make_rows([0, 1]) + make_rows([1]))
    assert report.duplicates == 1
    assert "Duplicate rows dropped: 1" in format_report(report)


def test_all_stations_gap_is_marked():
    # Three stations all miss 08.10. 01:00 UTC; 2087 also misses hour 5 alone.
    hours = [h for h in range(8) if h != 1]
    rows = (
        make_rows([h for h in hours if h != 5], station="2087")
        + make_rows(hours, station="2491")
        + make_rows(hours, station="2056", parameter="Q")
    )
    report = report_for(rows)
    assert report.all_station_hours == [pd.Timestamp(BAFU_HOLE)]
    flags = {(g.station, g.start.hour): g.all_stations for g in report.gaps.itertuples()}
    assert flags == {("2087", 1): True, ("2087", 5): False, ("2491", 1): True, ("2056", 1): True}


def test_station_is_missing_only_if_all_parameters_are_missing():
    # 2056 has W at hour 1, so the hour is not missing for all stations.
    rows = (
        make_rows([0, 2], station="2087")
        + make_rows([0, 2], station="2056", parameter="Q")
        + make_rows([0, 1, 2], station="2056", parameter="W")
    )
    assert report_for(rows).all_station_hours == []


def test_finished_series_does_not_cover_later_hours():
    # 2299 stops after hour 3 (days late). The hole at hour 5 is still a hole for the others.
    rows = (
        make_rows([0, 1, 2, 3], station="2299")
        + make_rows([0, 1, 2, 3, 4, 6], station="2087")
        + make_rows([0, 1, 2, 3, 4, 6], station="2491")
    )
    assert report_for(rows).all_station_hours == [pd.Timestamp(START + timedelta(hours=5))]


def test_single_station_gap_is_not_an_all_stations_gap():
    assert report_for(make_rows([0, 3])).all_station_hours == []


def test_empty_grid():
    report = report_for([])
    assert report.gaps.empty
    assert report.summary.empty
    assert report.all_station_hours == []
    assert "Hours missing for all stations: 0" in format_report(report)


def test_format_report_lists_the_gap():
    text = format_report(report_for(make_rows([0, 3], station="2087")))
    assert "2087 W: 1 gaps, longest 2 h" in text
    assert "2026-10-08 01:00 to 2026-10-08 02:00 UTC, 2 h" in text
