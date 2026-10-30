"""Tests for the lag and rolling features: lags use timestamps and nothing leaks from the future.

The leakage tests also run on series that contain a spike and a flat run: the features use
the causal cleaning checks, which never look at later hours.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from flowguard.common.features import (
    CALENDAR,
    TARGET,
    build_features,
    clean_grid,
    feature_columns,
    series_columns,
    table_columns,
)
from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.stations import load_stations
from flowguard.features.hourly_grid import build_hourly_grid

START = datetime(2026, 1, 1, tzinfo=UTC)
FETCHED = datetime(2026, 2, 1, tzinfo=UTC)
HOURS = 500
# (station, parameter) -> (base value, step size). W in m a.s.l., Q in m3/s, inside the limits.
SERIES = {
    ("2087", "W"): (1426.8, 0.005),
    ("2087", "Q"): (20.0, 0.05),
    ("2491", "W"): (486.0, 0.005),
    ("2491", "Q"): (10.0, 0.05),
    ("2056", "W"): (438.0, 0.005),
    ("2056", "Q"): (50.0, 0.05),
    ("2056", "WT"): (8.0, 0.01),
    ("2492", "Q"): (1.0, 0.01),
    ("2649", "Q"): (5.0, 0.05),
}


@pytest.fixture(scope="module")
def stations():
    return load_stations()


@pytest.fixture(scope="module")
def cleaning():
    return load_pipeline_config().cleaning


def make_values(seed=0):
    rng = np.random.default_rng(seed)
    return {
        key: base + np.cumsum(rng.normal(0, step, HOURS)) for key, (base, step) in SERIES.items()
    }


def make_grid(values, drop=(), release="validated"):
    """Hourly grid from arrays; drop is a set of (station, parameter, hour) left out."""
    rows = []
    for (station, parameter), array in values.items():
        for h, value in enumerate(array):
            if (station, parameter, h) in drop:
                continue
            rows.append(
                {
                    "station": station,
                    "parameter": parameter,
                    "timestamp": START + timedelta(hours=h),
                    "value": float(value),
                    "unit": "m",
                    "release_state": release,
                    "fetched_at": FETCHED,
                }
            )
    return build_hourly_grid(rows).grid


# Hours where make_events_values puts a one hour spike and a flat run.
SPIKE_HOUR = 300
FLAT_FROM, FLAT_TO = 350, 420


def make_events_values(seed=0):
    """Like make_values, plus a 2 m spike in Seedorf W and a 70 h flat run in Andermatt Q."""
    values = make_values(seed)
    values[("2056", "W")][SPIKE_HOUR] += 2.0
    values[("2087", "Q")][FLAT_FROM:FLAT_TO] = values[("2087", "Q")][FLAT_FROM]
    return values


def build(values, stations, cleaning, **kwargs):
    return build_features(make_grid(values, **kwargs), stations, cleaning)


def at(table, station, hour):
    """The row of one station at START + hour."""
    t = pd.Timestamp(START + timedelta(hours=int(hour)))
    row = table[(table["station_id"] == station) & (table["t"] == t)]
    assert len(row) == 1
    return row.iloc[0]


def features_at(table, stations, station, hour):
    return at(table, station, hour)[feature_columns(stations)].astype("float64")


def random_origins(seed, n=5):
    # Late enough that the 7 day difference and the 72 h window exist.
    return np.random.default_rng(seed).integers(200, HOURS - 30, size=n)


# (a) Truncation


# Around the spike, at the hour the flat run completes (FLAT_FROM + 47) and after it.
EVENT_ORIGINS = [299, 300, 301, 302, 349, 396, 397, 398, 399, 419, 420, 421]


@pytest.mark.parametrize("station", ["2087", "2491", "2056"])
def test_features_do_not_change_when_the_series_is_cut_after_t(station, stations, cleaning):
    values = make_events_values()
    full = build(values, stations, cleaning)
    for hour in [*random_origins(1), *EVENT_ORIGINS]:
        cut = {key: array[: hour + 1] for key, array in values.items()}
        short = build(cut, stations, cleaning)
        expected = features_at(full, stations, station, hour)
        actual = features_at(short, stations, station, hour)
        # The row label (position in the table) differs, the values must not.
        pd.testing.assert_series_equal(actual, expected, check_names=False)


def test_seedorf_features_are_complete_at_the_tested_origins(stations, cleaning):
    # Guards the comparisons above: equal NaN rows would prove nothing.
    table = build(make_values(), stations, cleaning)
    for hour in random_origins(1):
        assert not features_at(table, stations, "2056", hour).isna().any()


# (b) Future perturbation


def test_random_values_after_t_leave_the_features_of_t_unchanged(stations, cleaning):
    values = make_events_values()
    full = build(values, stations, cleaning)
    rng = np.random.default_rng(2)
    for hour in [*random_origins(3), *EVENT_ORIGINS]:
        changed = {}
        for key, array in values.items():
            base, step = SERIES[key]
            array = array.copy()
            array[hour + 1 :] = base + rng.uniform(-10, 10, len(array) - hour - 1) * step
            # Future spike right after t and a flat run after it: neither may reach back.
            array[hour + 1] += 2.0
            array[hour + 3 : hour + 80] = array[hour + 3]
            changed[key] = array
        perturbed = build(changed, stations, cleaning)
        for station in ("2087", "2491", "2056"):
            pd.testing.assert_series_equal(
                features_at(perturbed, stations, station, hour),
                features_at(full, stations, station, hour),
                check_names=False,
            )


# (c) Gaps


def test_a_missing_hour_gives_a_missing_difference_and_no_shifted_lag(stations, cleaning):
    values = make_values()
    gap = 300
    table = build(values, stations, cleaning, drop={("2056", "W", gap)})
    w = values[("2056", "W")] * 100

    # t - 24 h is the gap: missing, not the value of the hour before.
    assert pd.isna(at(table, "2056", gap + 24)["w_d24h"])
    # One hour either side the lags still point at the right hours.
    assert at(table, "2056", gap + 23)["w_d24h"] == pytest.approx(w[gap + 23] - w[gap - 1])
    assert at(table, "2056", gap + 25)["w_d24h"] == pytest.approx(w[gap + 25] - w[gap + 1])
    # The gap hour itself has no current value and no 1 h difference, nor has the next hour.
    assert pd.isna(at(table, "2056", gap)["w_now"])
    assert pd.isna(at(table, "2056", gap)["w_d1h"])
    assert pd.isna(at(table, "2056", gap + 1)["w_d1h"])
    # 6 h, 72 h and 7 d lags hit the gap at their own offsets and nowhere else.
    assert pd.isna(at(table, "2056", gap + 6)["w_d6h"])
    assert pd.isna(at(table, "2056", gap + 72)["w_d72h"])
    assert pd.isna(at(table, "2056", gap + 168)["w_d7d"])
    assert not pd.isna(at(table, "2056", gap + 7)["w_d6h"])


def test_the_gap_does_not_touch_other_series_or_stations(stations, cleaning):
    values = make_values()
    full = build(values, stations, cleaning)
    gapped = build(values, stations, cleaning, drop={("2056", "W", 300)})
    for column in ["q_d24h", "up_main_w_d24h", "div_q_2492_d24h"]:
        pd.testing.assert_series_equal(
            gapped.loc[gapped["station_id"] == "2056", column],
            full.loc[full["station_id"] == "2056", column],
        )
    assert gapped[gapped["station_id"] == "2087"].equals(full[full["station_id"] == "2087"])


def test_rolling_window_needs_75_percent_of_the_hours(stations, cleaning):
    values = make_values()
    t = 300
    w = values[("2056", "W")] * 100

    # 24 h window: 18 of 24 hours present is enough, 17 is not.
    enough = {("2056", "W", h) for h in range(t - 5, t + 1)}
    table = build(values, stations, cleaning, drop=enough)
    expected = w[t - 23 : t - 5]
    assert len(expected) == 18
    row = at(table, "2056", t)
    assert row["w_roll24_mean"] == pytest.approx(expected.mean())
    assert row["w_roll24_min"] == pytest.approx(expected.min())
    assert row["w_roll24_max"] == pytest.approx(expected.max())
    assert row["w_roll24_std"] == pytest.approx(expected.std(ddof=1))

    too_few = enough | {("2056", "W", t - 6)}
    table = build(values, stations, cleaning, drop=too_few)
    row = at(table, "2056", t)
    assert row[["w_roll24_mean", "w_roll24_std", "w_roll24_min", "w_roll24_max"]].isna().all()
    # The 72 h window still has 66 of 72 hours (54 needed).
    assert not pd.isna(row["w_roll72_mean"])


def test_rolling_window_ends_at_t_and_holds_the_last_24_and_72_hours(stations, cleaning):
    values = make_values()
    table = build(values, stations, cleaning)
    q = values[("2056", "Q")]
    row = at(table, "2056", 400)
    assert row["q_roll24_mean"] == pytest.approx(q[377:401].mean())
    assert row["q_roll72_max"] == pytest.approx(q[329:401].max())


# (d) Target


def test_target_is_the_24_hour_change_in_cm(stations, cleaning):
    values = make_values()
    table = build(values, stations, cleaning)
    w = values[("2087", "W")]
    for hour in (10, 250, 400):
        assert at(table, "2087", hour)[TARGET] == pytest.approx((w[hour + 24] - w[hour]) * 100)


def test_target_uses_only_t_and_t_plus_24h(stations, cleaning):
    values = make_values()
    hour = 250
    base = at(build(values, stations, cleaning), "2056", hour)[TARGET]

    other = {key: array.copy() for key, array in values.items()}
    for h in (hour - 1, hour + 1, hour + 23, hour + 25):
        other[("2056", "W")][h] += 0.1
    assert at(build(other, stations, cleaning), "2056", hour)[TARGET] == pytest.approx(base)

    other[("2056", "W")][hour + 24] += 0.1
    assert at(build(other, stations, cleaning), "2056", hour)[TARGET] == pytest.approx(base + 10)


def test_target_is_missing_if_either_end_is_missing(stations, cleaning):
    values = make_values()
    table = build(values, stations, cleaning, drop={("2087", "W", 250), ("2087", "W", 324)})
    assert pd.isna(at(table, "2087", 250)[TARGET])  # t missing
    assert pd.isna(at(table, "2087", 300)[TARGET])  # t + 24 h missing
    assert not pd.isna(at(table, "2087", 301)[TARGET])


def test_newest_24_origins_have_no_target_but_stay_in_the_table(stations, cleaning):
    table = build(make_values(), stations, cleaning)
    rows = table[table["station_id"] == "2087"]
    assert len(rows) == HOURS
    assert rows[TARGET].iloc[-24:].isna().all()
    assert rows[TARGET].iloc[:-24].notna().all()


# (e) Columns


def test_all_stations_have_the_same_columns_in_the_same_order(stations, cleaning):
    table = build(make_values(), stations, cleaning)
    assert list(table.columns) == table_columns(stations)
    assert sorted(table["station_id"].unique()) == ["2056", "2087", "2491"]
    counts = table.groupby("station_id").size()
    assert counts.nunique() == 1


def test_slots_without_a_station_are_missing(stations, cleaning):
    table = build(make_values(), stations, cleaning)
    andermatt = table[table["station_id"] == "2087"]
    seedorf = table[table["station_id"] == "2056"]
    for prefix in ("up_main_w", "up_main_q", "up_trib_w", "div_q_2492", "div_q_2649"):
        assert andermatt[series_columns(prefix)].isna().all().all()
        assert seedorf[series_columns(prefix)].iloc[200:].notna().all().all()
    buerglen = table[table["station_id"] == "2491"]
    assert buerglen[series_columns("up_main_w")].isna().all().all()
    assert seedorf["up_main_w_now"].iloc[300] == pytest.approx(
        make_values()[("2087", "W")][300] * 100
    )


def test_column_names_are_complete_and_unique(stations):
    names = table_columns(stations)
    assert len(names) == len(set(names))
    # 8 series x (now + 5 differences + 8 rolling), calendar, 4 metadata, target.
    assert len(feature_columns(stations)) == 8 * 14 + len(CALENDAR)
    assert len(names) == 4 + 8 * 14 + 2 + 1
    for expected in ["w_now", "w_d7d", "w_roll72_std", "up_trib_q_d6h", "div_q_2649_roll24_max"]:
        assert expected in names


# Causal cleaning of the features, label from the whole series


def test_a_spike_stays_in_the_features_but_a_spike_24h_ahead_has_no_target(stations, cleaning):
    values = make_events_values()
    table = build(values, stations, cleaning)
    w = values[("2056", "W")]
    # The feature at the spike hour is the spike itself (nothing is known about t + 1 yet).
    assert at(table, "2056", SPIKE_HOUR)["w_now"] == pytest.approx(w[SPIKE_HOUR] * 100)
    # The label at t = spike - 24 h would be the spike: flagged, so missing.
    assert pd.isna(at(table, "2056", SPIKE_HOUR - 24)[TARGET])
    # Neighbouring origins are fine.
    assert not pd.isna(at(table, "2056", SPIKE_HOUR - 25)[TARGET])
    assert not pd.isna(at(table, "2056", SPIKE_HOUR - 23)[TARGET])


def test_target_starts_from_the_value_the_features_see(stations, cleaning):
    values = make_events_values()
    table = build(values, stations, cleaning)
    w = values[("2056", "W")]
    row = at(table, "2056", SPIKE_HOUR)
    # t is the spike: the target is measured from w_now, the same value the model sees.
    assert row[TARGET] == pytest.approx(w[SPIKE_HOUR + 24] * 100 - row["w_now"])


def test_a_flat_run_is_flagged_from_the_completing_hour_in_the_features(stations, cleaning):
    values = make_events_values()
    table = build(values, stations, cleaning)
    last_ok = FLAT_FROM + 46  # 47 identical hours
    assert not pd.isna(at(table, "2087", last_ok)["q_now"])
    assert pd.isna(at(table, "2087", last_ok + 1)["q_now"])  # 48th identical hour
    assert pd.isna(at(table, "2087", FLAT_TO - 1)["q_now"])
    assert not pd.isna(at(table, "2087", FLAT_TO)["q_now"])


def test_run_report_flags_stay_non_causal(cleaning):
    values = make_events_values()
    _, flags = clean_grid(make_grid(values), cleaning)
    assert flags[("2056", "W")].iloc[SPIKE_HOUR] == "spike"
    assert flags[("2087", "Q")].iloc[FLAT_FROM] == "flat"


# Cleaning, units, calendar, metadata


def test_flagged_values_do_not_reach_the_features(stations, cleaning):
    values = make_values()
    values[("2087", "W")][300] = 9999.0  # far above the physical limit
    table = build(values, stations, cleaning)
    assert pd.isna(at(table, "2087", 300)["w_now"])
    assert pd.isna(at(table, "2087", 300)[TARGET])
    assert pd.isna(at(table, "2087", 324)["w_d24h"])
    # Seedorf takes Andermatt as up_main, so it sees the cleaned series as well.
    assert pd.isna(at(table, "2056", 300)["up_main_w_now"])


def test_clean_grid_uses_station_rules_and_skips_water_temperature(cleaning):
    values = make_values()
    values[("2492", "Q")][100] = 50.0  # above the 3 m3/s limit of 2492 only
    values[("2056", "Q")][100] = 50.0  # inside the limits of 2056
    series, flags = clean_grid(make_grid(values), cleaning)
    assert pd.isna(series[("2492", "Q")].iloc[100])
    assert flags[("2492", "Q")].iloc[100] == "range"
    assert series[("2056", "Q")].iloc[100] == 50.0
    assert ("2056", "WT") not in series
    indexes = {tuple(s.index) for s in series.values()}
    assert len(indexes) == 1


def test_levels_are_in_cm_and_discharge_is_unchanged(stations, cleaning):
    values = make_values()
    row = at(build(values, stations, cleaning), "2056", 250)
    assert row["w_now"] == pytest.approx(values[("2056", "W")][250] * 100)
    assert row["q_now"] == pytest.approx(values[("2056", "Q")][250])
    assert row["div_q_2492_now"] == pytest.approx(values[("2492", "Q")][250])


def test_day_of_year_is_a_sine_cosine_pair(stations, cleaning):
    table = build(make_values(), stations, cleaning)
    row = at(table, "2056", 24)  # 2 January, day of year 2
    angle = 2 * np.pi * 2 / 365.25
    assert row["doy_sin"] == pytest.approx(np.sin(angle))
    assert row["doy_cos"] == pytest.approx(np.cos(angle))


def test_release_state_is_kept_at_t_and_at_t_plus_24h(stations, cleaning):
    values = make_values()
    grid = make_grid(values)
    mask = (grid["station"] == "2056") & (grid["parameter"] == "W")
    stamp = pd.Timestamp(START + timedelta(hours=250))
    grid.loc[mask & (grid["timestamp"] == stamp), "release_state"] = None
    table = build_features(grid, stations, cleaning)
    assert pd.isna(at(table, "2056", 250)["w_release_state"])
    assert at(table, "2056", 250)["y_release_state"] == "validated"
    assert pd.isna(at(table, "2056", 226)["y_release_state"])
    assert at(table, "2056", 226)["w_release_state"] == "validated"


def test_empty_grid_gives_an_empty_table_with_all_columns(stations, cleaning):
    empty = make_grid({})
    table = build_features(empty, stations, cleaning)
    assert table.empty
    assert list(table.columns) == table_columns(stations)
