"""Tests for the hourly grid. Rows are built by hand, no network."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from helpers import FETCHED, START, make_rows

from flowguard.features.bafu_client import to_row
from flowguard.features.hourly_grid import COLUMNS, build_hourly_grid

ZURICH = ZoneInfo("Europe/Zurich")
RECORDED = Path(__file__).parent / "fixtures" / "bafu_hourly_2056.json"


def test_no_gap():
    result = build_hourly_grid(make_rows(range(5)))
    grid = result.grid
    assert len(grid) == 5
    assert not grid["is_missing"].any()
    assert list(grid["value"]) == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert result.duplicates == 0
    assert list(grid.columns) == COLUMNS


def test_one_hour_gap_stays_missing():
    grid = build_hourly_grid(make_rows([0, 1, 3, 4])).grid
    assert len(grid) == 5
    gap = grid.iloc[2]
    assert gap["is_missing"]
    assert gap["timestamp"] == pd.Timestamp(START + timedelta(hours=2))
    # Never filled: no value, no release state, no fetch time.
    assert pd.isna(gap["value"])
    assert pd.isna(gap["release_state"])
    assert pd.isna(gap["fetched_at"])
    assert grid["is_missing"].sum() == 1


def test_multi_hour_gap():
    grid = build_hourly_grid(make_rows([0, 1, 6, 7])).grid
    assert len(grid) == 8
    assert list(grid["is_missing"]) == [False, False, True, True, True, True, False, False]
    assert grid["value"].isna().sum() == 4


def test_duplicates_keep_latest_fetch_and_are_counted():
    old = make_rows([0, 1, 2], fetched_at=FETCHED)
    new = make_rows([1, 2], fetched_at=FETCHED + timedelta(hours=1), release_state="validated")
    for row in new:
        row["value"] += 100
    for rows in (old + new, new + old):  # input order does not matter
        result = build_hourly_grid(rows)
        assert result.duplicates == 2
        assert list(result.grid["value"]) == [0.0, 101.0, 102.0]
        assert list(result.grid["release_state"].iloc[1:]) == ["validated", "validated"]


def test_keeps_release_state_and_fetched_at():
    rows = make_rows([0, 1], release_state="validated")
    grid = build_hourly_grid(rows).grid
    assert list(grid["release_state"]) == ["validated", "validated"]
    assert (grid["fetched_at"] == pd.Timestamp(FETCHED)).all()


def test_null_value_counts_as_missing():
    rows = make_rows([0, 1, 2])
    rows[1]["value"] = None
    grid = build_hourly_grid(rows).grid
    assert list(grid["is_missing"]) == [False, True, False]


@pytest.mark.parametrize(
    "bad",
    [
        datetime(2026, 10, 8, 3),  # noqa: DTZ001 (naive on purpose)
        datetime(2026, 10, 8, 3, tzinfo=timezone(timedelta(hours=2))),  # not UTC
        datetime(2026, 10, 8, 3, 30, tzinfo=UTC),  # not on the hour
        datetime(2026, 10, 8, 3, 0, 1, tzinfo=UTC),  # seconds
    ],
)
def test_bad_timestamp_raises(bad):
    rows = make_rows([0, 1])
    rows[1]["timestamp"] = bad
    with pytest.raises(ValueError):
        build_hourly_grid(rows)


def test_bad_window_raises():
    with pytest.raises(ValueError):
        build_hourly_grid(make_rows([0]), start=datetime(2026, 10, 8))  # noqa: DTZ001


def test_empty_input():
    result = build_hourly_grid([])
    assert result.grid.empty
    assert list(result.grid.columns) == COLUMNS
    assert result.duplicates == 0


def test_series_are_separate():
    rows = make_rows([0, 1, 2], station="2087") + make_rows(
        [0, 2], station="2056", parameter="Q", start=START + timedelta(hours=1)
    )
    grid = build_hourly_grid(rows).grid
    q = grid[grid["station"] == "2056"]
    assert len(q) == 3
    assert list(q["is_missing"]) == [False, True, False]
    assert (q["parameter"] == "Q").all()
    assert len(grid[grid["station"] == "2087"]) == 3


def test_window_counts_trailing_gap():
    rows = make_rows([0, 1, 2])
    grid = build_hourly_grid(rows, start=START, end=START + timedelta(hours=5)).grid
    assert len(grid) == 5
    assert list(grid["is_missing"]) == [False, False, False, True, True]


def test_default_span_ignores_publication_delay():
    # No rows after the newest value, so no trailing missing hours.
    grid = build_hourly_grid(make_rows([0, 1, 2])).grid
    assert len(grid) == 3


def test_mixed_units_raise():
    rows = make_rows([0, 1])
    rows[1]["unit"] = "cm"
    with pytest.raises(ValueError):
        build_hourly_grid(rows)


@pytest.mark.parametrize(
    "local_start, local_end, expected_hours",
    [
        (datetime(2026, 3, 28, tzinfo=ZURICH), datetime(2026, 3, 30, tzinfo=ZURICH), 47),
        (datetime(2026, 10, 24, tzinfo=ZURICH), datetime(2026, 10, 26, tzinfo=ZURICH), 49),
    ],
)
def test_daylight_saving_change_loses_and_doubles_no_hour(local_start, local_end, expected_hours):
    # Local midnights of the 23 h and the 25 h day, walked in UTC steps like the real hours.
    now = local_start.astimezone(UTC)
    stop = local_end.astimezone(UTC)
    hours = int((stop - now) / timedelta(hours=1))
    assert hours == expected_hours
    rows = make_rows(range(hours), start=now)
    result = build_hourly_grid(rows)
    grid = result.grid
    assert len(grid) == expected_hours
    assert grid["timestamp"].is_unique
    assert not grid["is_missing"].any()
    assert (grid["timestamp"].diff().dropna() == pd.Timedelta(hours=1)).all()


def test_recorded_response_rows_build_a_grid():
    records = json.loads(RECORDED.read_text())["data"]["water"]["observations"]["data_1hour_mean"]
    rows = [to_row("2056", r, FETCHED) for r in records]
    grid = build_hourly_grid(rows).grid
    # 3 parameters x 3 hours, no gaps
    assert len(grid) == 9
    assert not grid["is_missing"].any()
    assert set(grid["parameter"]) == {"Q", "W", "WT"}
