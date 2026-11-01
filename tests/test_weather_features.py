"""Tests for the weather composite series and the weather features.

The synthetic runs encode where a value came from: value = point * 100000 + variable * 10000
+ run * 100 + lead, with run = number of 6 h cycles since BASE. So the value in a feature
tells which run and lead it was taken from.
"""

import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from functools import partial

import numpy as np
import pandas as pd
import pytest

from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.weather_features import (
    FEATURES,
    MAX_LEAD,
    RUN_AGE_COLUMN,
    build_weather_features,
    composite_windows,
    load_runs,
    select_run_starts,
    weather_columns,
)
from flowguard.features.weather_client import VARIABLES, select_run

BASE = datetime(2026, 10, 1, tzinfo=UTC)
FETCHED = datetime(2026, 10, 20, tzinfo=UTC)
N_RUNS = 48  # 12 days of runs
NAMES = ("andermatt", "meiental", "schaechental")
CYCLE = timedelta(hours=6)


@pytest.fixture
def config():
    # weather_start 2026-10-01 -> first_weather_origin 2026-10-04 00:00 UTC
    return replace(load_pipeline_config(), weather_start=date(2026, 10, 1))


def value(point, variable, run, lead):
    return float(point * 100000 + variable * 10000 + run * 100 + lead)


def make_rows(runs, bump=0.0, fetched_at=FETCHED):
    """Raw weather rows (leads 1 to 47) for the given run numbers."""
    rows = []
    for run in runs:
        start = BASE + run * CYCLE
        for p, name in enumerate(NAMES):
            for v, parameter in enumerate(VARIABLES):
                for lead in range(1, MAX_LEAD + 1):
                    rows.append(
                        {
                            "point": name,
                            "valid_time": start + timedelta(hours=lead),
                            "run_start": start,
                            "parameter": parameter,
                            "value": value(p, v, run, lead) + bump,
                            "unit": "x",
                            "fetched_at": fetched_at,
                        }
                    )
    return rows


def run_number(run_start):
    return int((run_start - BASE) / CYCLE)


def never_null(p, v, run, lead):
    return False


def reference_value(p, v, selected, rel, stored, is_null=never_null):
    """Value of the composite at S + rel hours, written as plainly as possible.

    selected: run number S (already a stored run). stored: set of stored run numbers.
    is_null(point, variable, run, lead): True where the stored run has no value.
    """
    if rel <= 6:
        # Up to S + 6 h: the run that started just before the valid time.
        run = selected + (rel - 1) // 6
        lead = rel - 6 * ((rel - 1) // 6)
    else:
        # After S + 6 h: run S itself.
        run, lead = selected, rel
    # A run covers the hour only if it is stored and has a value there; else the next older run.
    while lead <= MAX_LEAD:
        if run in stored and not is_null(p, v, run, lead):
            return value(p, v, run, lead)
        run, lead = run - 1, lead + 6
    return np.nan


def reference_window(p, v, selected, d, stored, first, last, is_null=never_null):
    """Composite values for the valid times c + first .. c + last (c = S + d + 1 hours)."""
    return [
        reference_value(p, v, selected, d + 1 + h, stored, is_null) for h in range(first, last + 1)
    ]


def reference_features(selected, d, stored, is_null=never_null):
    """The 12 features of one point, computed hour by hour from the reference composite."""
    out = {}
    for p in range(len(NAMES)):
        prec = partial(reference_window, p, 0, selected, d, stored, is_null=is_null)
        temp = partial(reference_window, p, 1, selected, d, stored, is_null=is_null)
        snow = reference_window(p, 2, selected, d, stored, 1, 24, is_null)
        out[p] = {
            "prec_obs_24h": np.sum(prec(-23, 0)),
            "prec_obs_48h": np.sum(prec(-47, 0)),
            "prec_obs_72h": np.sum(prec(-71, 0)),
            "tmean_obs_24h": np.mean(temp(-23, 0)),
            "tmax_obs_24h": np.max(temp(-23, 0)),
            "prec_fc_next6h": np.sum(prec(1, 6)),
            "prec_fc_next12h": np.sum(prec(1, 12)),
            "prec_fc_next24h": np.sum(prec(1, 24)),
            "prec_fc_peak24h": np.max(prec(1, 24)),
            "tmean_fc_24h": np.mean(temp(1, 24)),
            "tmax_fc_24h": np.max(temp(1, 24)),
            "snow_fc_next24h": np.sum(snow),
        }
    return out


def with_nulls(rows, is_null):
    """The rows with value None where is_null(point, variable, run, lead) is true."""
    out = []
    for r in rows:
        p, v = NAMES.index(r["point"]), VARIABLES.index(r["parameter"])
        run, lead = (
            run_number(r["run_start"]),
            int((r["valid_time"] - r["run_start"]).total_seconds() // 3600),
        )
        out.append({**r, "value": None} if is_null(p, v, run, lead) else r)
    return out


def assert_row_equals_reference(row, expected):
    for p, name in enumerate(NAMES):
        for suffix, *_ in FEATURES:
            got, want = row[f"wx_{name}_{suffix}"], expected[p][suffix]
            assert (np.isnan(got) and np.isnan(want)) or got == want, (name, suffix, got, want)


def build(rows, origins, config):
    return build_weather_features(rows, pd.DatetimeIndex(origins), config)


def row_at(frame, t):
    return frame[frame["t"] == pd.Timestamp(t)].iloc[0]


T = datetime(2026, 10, 8, 10, tzinfo=UTC)  # first origin of the normal cases, S = 06:00 same day


def selected_run_number(t, config):
    return run_number(
        select_run(t, as_of_offset=config.as_of_offset, min_run_age=config.min_run_age)
    )


# --- composite series -------------------------------------------------------------------


def test_composite_series_on_a_hand_built_example(config):
    cube = load_runs(make_rows(range(N_RUNS)), config.weather_points)
    s = 20  # run 2026-10-06 00Z
    d = 4
    window = composite_windows(cube, [s], d)
    assert window.shape == (3, 3, 1, 96)

    # rel = valid time - S in hours = d - 70 + index. Precipitation of the first point.
    def cell(rel):
        return window[0, 0, 0, rel - (d - 70)]

    # Up to S + 6 h: leads 1 to 6 of the run that started just before the valid time.
    assert cell(6) == value(0, 0, 20, 6)
    assert cell(5) == value(0, 0, 20, 5)
    assert cell(1) == value(0, 0, 20, 1)
    assert cell(0) == value(0, 0, 19, 6)
    assert cell(-5) == value(0, 0, 19, 1)
    assert cell(-6) == value(0, 0, 18, 6)
    assert cell(-66) == value(0, 0, 8, 6)
    # After S + 6 h: run S itself, leads 7 and up, also after c.
    assert cell(7) == value(0, 0, 20, 7)
    assert cell(d + 1) == value(0, 0, 20, 5)  # c itself is still inside S + 6 h
    assert cell(d + 25) == value(0, 0, 20, 29)
    # Other point and variable are read from their own slice.
    assert window[2, 1, 0, 7 - (d - 70)] == value(1, 2, 20, 7)


def test_composite_series_matches_the_plain_reference_for_every_cell(config):
    stored = set(range(N_RUNS))
    cube = load_runs(make_rows(stored), config.weather_points)
    for d in (4, 7, 9):
        window = composite_windows(cube, [20, 21], d)
        for k, s in enumerate((20, 21)):
            for p in range(3):
                for v in range(3):
                    expected = [
                        reference_value(p, v, s, rel, stored) for rel in range(d - 70, d + 26)
                    ]
                    np.testing.assert_array_equal(window[v, p, k], expected)


# --- features ---------------------------------------------------------------------------


def test_features_equal_the_plain_reference_for_all_three_points(config):
    stored = set(range(N_RUNS))
    origins = [T + timedelta(hours=h) for h in range(12)]  # two full cycles, six offsets
    frame = build(make_rows(stored), origins, config)
    assert list(frame.columns) == ["t", *weather_columns(config.weather_points)]
    for t in origins:
        s = selected_run_number(t, config)
        d = int((t - (BASE + s * CYCLE)) / timedelta(hours=1))
        expected = reference_features(s, d, stored)
        row = row_at(frame, t)
        assert row[RUN_AGE_COLUMN] == d
        for p, name in enumerate(NAMES):
            for suffix, *_ in FEATURES:
                assert row[f"wx_{name}_{suffix}"] == expected[p][suffix], (t, name, suffix)


def test_run_age_is_four_to_nine_hours_in_the_normal_case(config):
    origins = [T + timedelta(hours=h) for h in range(24)]
    frame = build(make_rows(range(N_RUNS)), origins, config)
    assert set(frame[RUN_AGE_COLUMN]) == {4, 5, 6, 7, 8, 9}


def test_observed_part_ends_at_c_and_forecast_part_starts_after_it(config):
    # prec value = run * 100 + lead, so one hour at a time can be told apart.
    stored = set(range(N_RUNS))
    t = T  # S = 06:00, d = 4, c = t + 1 h = S + 5 h
    row = row_at(build(make_rows(stored), [t], config), t)
    s = selected_run_number(t, config)
    # Next-6h sum = valid times c+1 .. c+6 = S+6 (run S lead 6) and S+7..S+11 (leads 7..11).
    expected = value(0, 0, s, 6) + sum(value(0, 0, s, lead) for lead in range(7, 12))
    assert row["wx_andermatt_prec_fc_next6h"] == expected


# --- availability and truncation --------------------------------------------------------


def test_a_run_that_started_after_t_minus_4h_changes_nothing(config):
    t = T
    s = selected_run_number(t, config)
    base = build(make_rows(range(N_RUNS)), [t], config)
    later = [r for r in range(N_RUNS) if r > s]  # these started after t - 4 h
    assert all(BASE + r * CYCLE > t - timedelta(hours=4) for r in later)

    without = build(make_rows(r for r in range(N_RUNS) if r <= s), [t], config)
    changed = build(
        make_rows(r for r in range(N_RUNS) if r <= s) + make_rows(later, bump=777.0), [t], config
    )
    pd.testing.assert_frame_equal(base, without)
    pd.testing.assert_frame_equal(base, changed)


def test_the_selected_run_and_older_runs_do_change_the_row(config):
    t = T
    s = selected_run_number(t, config)
    base = build(make_rows(range(N_RUNS)), [t], config)
    for run in (s, s - 1, s - 5):
        rows = make_rows(r for r in range(N_RUNS) if r != run) + make_rows([run], bump=1.0)
        other = build(rows, [t], config)
        assert not base.equals(other), run


def test_cutting_the_store_at_the_selected_run_gives_the_same_rows(config):
    # Same idea as the truncation test of the BAFU features, for every origin of two days.
    origins = [T + timedelta(hours=h) for h in range(48)]
    full = build(make_rows(range(N_RUNS)), origins, config)
    for t in origins:
        s = selected_run_number(t, config)
        cut = build(make_rows(range(s + 1)), [t], config)
        pd.testing.assert_frame_equal(
            cut.reset_index(drop=True), full[full["t"] == pd.Timestamp(t)].reset_index(drop=True)
        )


def test_random_values_in_later_runs_leave_the_row_unchanged(config):
    rng = np.random.default_rng(0)
    t = T
    s = selected_run_number(t, config)
    rows = make_rows(range(N_RUNS))
    noisy = [
        {**r, "value": float(rng.random() * 50)} if r["run_start"] > BASE + s * CYCLE else r
        for r in rows
    ]
    pd.testing.assert_frame_equal(build(rows, [t], config), build(noisy, [t], config))


# --- first weather origin ---------------------------------------------------------------


def test_row_at_the_first_weather_origin_is_complete_and_the_row_before_is_empty(config):
    first = config.first_weather_origin
    assert first == datetime(2026, 10, 4, tzinfo=UTC)
    origins = [first - timedelta(hours=1), first, first + timedelta(hours=1)]
    frame = build(make_rows(range(N_RUNS)), origins, config)
    assert list(frame["t"]) == [pd.Timestamp(t) for t in origins]  # the row keeps its place
    columns = weather_columns(config.weather_points)
    assert frame.loc[0, columns].isna().all()
    assert frame.loc[1, columns].notna().all()
    assert frame.loc[2, columns].notna().all()


def test_runs_before_the_first_origin_do_not_give_earlier_rows_weather(config):
    # The runs exist (they start on 1 October), but the 72 h windows are not complete yet.
    early = [BASE + timedelta(days=2, hours=h) for h in (0, 12, 23)]
    frame = build(make_rows(range(N_RUNS)), early, config)
    assert frame[weather_columns(config.weather_points)].isna().all().all()


# --- missing runs -----------------------------------------------------------------------


def test_a_missing_selected_run_falls_back_to_the_newest_older_run(config):
    t = T
    s = selected_run_number(t, config)
    stored = set(range(N_RUNS)) - {s}
    row = row_at(build(make_rows(stored), [t], config), t)
    d = 4 + 6  # run age shows the real age
    assert row[RUN_AGE_COLUMN] == d
    expected = reference_features(s - 1, d, stored)
    for p, name in enumerate(NAMES):
        for suffix, *_ in FEATURES:
            assert row[f"wx_{name}_{suffix}"] == expected[p][suffix], (name, suffix)


def test_two_missing_runs_in_a_row_give_the_run_before_them(config):
    t = T
    s = selected_run_number(t, config)
    stored = set(range(N_RUNS)) - {s, s - 1}
    row = row_at(build(make_rows(stored), [t], config), t)
    assert row[RUN_AGE_COLUMN] == 4 + 12
    expected = reference_features(s - 2, 4 + 12, stored)
    assert row["wx_meiental_prec_obs_24h"] == expected[1]["prec_obs_24h"]


def test_a_missing_run_inside_the_72h_window_is_filled_from_the_older_run(config):
    t = T
    s = selected_run_number(t, config)
    missing = s - 3  # 18 h before S, inside the 72 h window
    stored = set(range(N_RUNS)) - {missing}
    row = row_at(build(make_rows(stored), [t], config), t)
    assert row[RUN_AGE_COLUMN] == 4  # S itself is there
    expected = reference_features(s, 4, stored)
    for p, name in enumerate(NAMES):
        for suffix, *_ in FEATURES:
            assert row[f"wx_{name}_{suffix}"] == expected[p][suffix], (name, suffix)
    # The 24 h sum holds hours of the missing run, taken at lead 7 to 12 of the run before it.
    full = row_at(build(make_rows(range(N_RUNS)), [t], config), t)
    assert row["wx_andermatt_prec_obs_72h"] != full["wx_andermatt_prec_obs_72h"]
    assert row["wx_andermatt_prec_obs_72h"] > 0 and not np.isnan(row["wx_andermatt_prec_obs_72h"])


def test_an_hour_no_stored_run_covers_is_missing_and_so_are_its_windows(config):
    # Runs only from number 2 (12:00 on 1 October): valid times before 13:00 are not covered.
    first = config.first_weather_origin
    stored = set(range(2, N_RUNS))
    row = row_at(build(make_rows(stored), [first], config), first)
    assert not np.isnan(row["wx_andermatt_prec_obs_48h"])
    assert np.isnan(row["wx_andermatt_prec_obs_72h"])
    # The forecast and the temperature of the last 24 h do not hold such an hour.
    assert not np.isnan(row["wx_andermatt_prec_fc_next24h"])
    assert not np.isnan(row["wx_andermatt_tmax_obs_24h"])


def test_no_selected_run_stored_at_all_gives_missing_values_and_no_error(config):
    t = T
    stored_late = range(30, N_RUNS)  # all runs are newer than the selected one
    row = row_at(build(make_rows(stored_late), [t], config), t)
    assert row[weather_columns(config.weather_points)].isna().all()


def test_a_stale_store_gives_missing_forecast_hours_not_a_wrong_run(config):
    # Newest stored run is 3 days older than S: its lead 7+ no longer reaches c + 24.
    t = datetime(2026, 10, 11, 12, tzinfo=UTC)
    row = row_at(build(make_rows(range(36)), [t], config), t)
    assert row[RUN_AGE_COLUMN] == 42
    assert np.isnan(row["wx_andermatt_prec_fc_next24h"])


# --- null values ------------------------------------------------------------------------


def test_an_all_null_variable_in_the_selected_run_is_read_from_the_older_run(config):
    t = T
    s = selected_run_number(t, config)
    precipitation_null = lambda p, v, run, lead: v == 0 and run == s
    rows = with_nulls(make_rows(range(N_RUNS)), precipitation_null)
    row = row_at(build(rows, [t], config), t)
    full = row_at(build(make_rows(range(N_RUNS)), [t], config), t)

    assert row[RUN_AGE_COLUMN] == 4  # run S is still the selected run
    expected = reference_features(s, 4, set(range(N_RUNS)), precipitation_null)
    assert_row_equals_reference(row, expected)
    # Precipitation after S + 6 h now comes from run S - 1 at lead 13 and up: values differ ...
    assert row["wx_andermatt_prec_fc_next24h"] != full["wx_andermatt_prec_fc_next24h"]
    assert not np.isnan(row["wx_andermatt_prec_fc_next24h"])
    # ... while temperature and snowfall of the same run are untouched.
    assert row["wx_andermatt_tmax_fc_24h"] == full["wx_andermatt_tmax_fc_24h"]
    assert row["wx_andermatt_snow_fc_next24h"] == full["wx_andermatt_snow_fc_next24h"]


def test_a_single_null_hour_in_the_selected_run_is_read_from_the_older_run(config):
    t = T  # S = 06:00, d = 4, the next 6 h are valid times S+6 .. S+11
    s = selected_run_number(t, config)
    hole = lambda p, v, run, lead: (p, v, run, lead) == (0, 0, s, 9)
    rows = with_nulls(make_rows(range(N_RUNS)), hole)
    row = row_at(build(rows, [t], config), t)
    full = row_at(build(make_rows(range(N_RUNS)), [t], config), t)

    assert_row_equals_reference(row, reference_features(s, 4, set(range(N_RUNS)), hole))
    # Only that hour changes: run S - 1 lead 15 instead of run S lead 9.
    change = value(0, 0, s - 1, 15) - value(0, 0, s, 9)
    assert row["wx_andermatt_prec_fc_next6h"] == full["wx_andermatt_prec_fc_next6h"] + change
    assert row["wx_andermatt_prec_fc_next24h"] == full["wx_andermatt_prec_fc_next24h"] + change
    # Other windows, the other points and the other variables do not change.
    assert row["wx_andermatt_prec_obs_72h"] == full["wx_andermatt_prec_obs_72h"]
    assert row["wx_meiental_prec_fc_next6h"] == full["wx_meiental_prec_fc_next6h"]
    assert row["wx_andermatt_tmean_fc_24h"] == full["wx_andermatt_tmean_fc_24h"]


def test_a_null_in_the_older_run_that_would_be_the_fallback_goes_one_run_further(config):
    t = T
    s = selected_run_number(t, config)
    missing = s - 3  # inside the 72 h window, its hours would come from run s - 4
    # Run s - 4 has no precipitation at lead 7 to 12 (the leads that replace the missing run).
    hole = lambda p, v, run, lead: v == 0 and run == missing - 1 and 7 <= lead <= 12
    stored = set(range(N_RUNS)) - {missing}
    row = row_at(build(with_nulls(make_rows(stored), hole), [t], config), t)

    assert_row_equals_reference(row, reference_features(s, 4, stored, hole))
    plain = row_at(build(make_rows(stored), [t], config), t)
    assert not np.isnan(row["wx_andermatt_prec_obs_72h"])  # run s - 5 at lead 13 to 18 covers it
    assert row["wx_andermatt_prec_obs_72h"] != plain["wx_andermatt_prec_obs_72h"]
    assert row["wx_andermatt_prec_fc_next24h"] == plain["wx_andermatt_prec_fc_next24h"]


def test_an_hour_no_run_has_a_value_for_makes_the_window_missing(config):
    t = T
    s = selected_run_number(t, config)
    # Precipitation of every run is null at the valid time S + 8, so no run covers it.
    valid = BASE + s * CYCLE + timedelta(hours=8)
    rows = [
        {**r, "value": None}
        if r["parameter"] == "precipitation" and r["valid_time"] == valid
        else r
        for r in make_rows(range(N_RUNS))
    ]
    row = row_at(build(rows, [t], config), t)
    assert np.isnan(row["wx_andermatt_prec_fc_next6h"])  # S + 8 lies in c + 1 .. c + 6
    assert np.isnan(row["wx_andermatt_prec_fc_next24h"])
    assert not np.isnan(row["wx_andermatt_prec_obs_72h"])  # S + 8 is after c
    assert not np.isnan(row["wx_andermatt_tmax_fc_24h"])


def test_nulls_in_runs_after_the_selected_run_change_nothing(config):
    t = T
    s = selected_run_number(t, config)
    base = build(make_rows(range(N_RUNS)), [t], config)
    all_null = lambda p, v, run, lead: run > s
    pd.testing.assert_frame_equal(
        base, build(with_nulls(make_rows(range(N_RUNS)), all_null), [t], config)
    )


# --- inputs and details -----------------------------------------------------------------


def test_select_run_starts_equals_select_run_for_every_hour(config):
    hours = pd.date_range("2026-10-01", periods=24 * 21, freq="h", tz="UTC")
    for pair in ((3, 7), (1, 5), (4, 12)):
        offset, age = (timedelta(hours=h) for h in pair)
        got = select_run_starts(hours, as_of_offset=offset, min_run_age=age)
        want = [select_run(t, as_of_offset=offset, min_run_age=age) for t in hours]
        assert list(got) == [pd.Timestamp(w) for w in want]


def test_origins_must_be_utc_and_on_the_hour(config):
    rows = make_rows(range(N_RUNS))
    with pytest.raises(ValueError, match="timezone"):
        build_weather_features(rows, pd.DatetimeIndex(["2026-10-08 10:00"]), config)
    with pytest.raises(ValueError, match="hour"):
        build(rows, [T + timedelta(minutes=30)], config)


def test_runs_off_the_six_hour_cycle_are_rejected(config):
    rows = make_rows([3])
    for r in rows:
        r["run_start"] = r["run_start"] + timedelta(hours=1)
    with pytest.raises(ValueError, match="cycle"):
        load_runs(rows, config.weather_points)


def test_empty_store_and_no_origins(config):
    frame = build([], [T], config)
    assert len(frame) == 1
    assert frame[weather_columns(config.weather_points)].isna().all().all()
    empty = build(make_rows(range(N_RUNS)), [], config)
    assert list(empty.columns) == ["t", *weather_columns(config.weather_points)]
    assert len(empty) == 0


def test_the_newest_stored_version_of_a_value_wins(config):
    old = make_rows(range(N_RUNS), bump=500.0, fetched_at=FETCHED - timedelta(days=1))
    new = make_rows(range(N_RUNS))
    both = build(old + new, [T], config)
    pd.testing.assert_frame_equal(both, build(new, [T], config))
    both_reversed = build(new + old, [T], config)
    pd.testing.assert_frame_equal(both_reversed, both)


def test_observed_rows_without_a_run_are_ignored(config):
    observed = [{**r, "run_start": None} for r in make_rows([10])]
    rows = make_rows(range(N_RUNS))
    pd.testing.assert_frame_equal(build(rows + observed, [T], config), build(rows, [T], config))


def test_origin_order_is_kept_and_duplicates_are_allowed(config):
    origins = [T + timedelta(hours=5), T, T + timedelta(hours=5)]
    frame = build(make_rows(range(N_RUNS)), origins, config)
    assert list(frame["t"]) == [pd.Timestamp(t) for t in origins]
    pd.testing.assert_series_equal(
        frame.iloc[0].drop("t"), frame.iloc[2].drop("t"), check_names=False
    )


def test_a_longer_history_builds_fast(config):
    # 120 days of runs, 2,800 origins: all grouped by the offset d, no loop per origin.
    n = 480
    rows = make_rows(range(n))
    origins = pd.date_range(config.first_weather_origin, periods=24 * 100, freq="h", tz="UTC")
    start = time.perf_counter()
    frame = build_weather_features(rows, origins, config)
    assert time.perf_counter() - start < 30
    assert frame[weather_columns(config.weather_points)[:-1]].notna().all().all()
