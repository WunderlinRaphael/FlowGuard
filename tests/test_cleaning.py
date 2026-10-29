import numpy as np
import pandas as pd
import pytest

from flowguard.common.cleaning import (
    CleaningConfig,
    CleaningRules,
    build_rules,
    clean_series,
    parse_cleaning_config,
    rules_for,
)

RULES = CleaningRules(min=0.0, max=100.0, max_step=10.0, flat_hours=5)


def series(values, start="2026-01-01 00:00"):
    index = pd.date_range(start, periods=len(values), freq="h", tz="UTC")
    return pd.Series(values, index=index, dtype="float64")


def flags_of(values, rules=RULES):
    return list(clean_series(series(values), rules).flags)


def test_clean_values_stay_ok():
    assert flags_of([1, 2, 3, 4, 5, 6]) == ["ok"] * 6


def test_range_flags_values_outside_the_limits():
    assert flags_of([1, 150, 2, -5, 3, 4]) == ["ok", "range", "ok", "range", "ok", "ok"]


def test_range_limits_are_inclusive():
    assert flags_of([0, 100, 50]) == ["ok", "ok", "ok"]


def test_one_hour_spike_is_flagged():
    assert flags_of([20, 20, 60, 20, 20]) == ["ok", "ok", "spike", "ok", "ok"]


def test_two_hour_spike_is_flagged():
    assert flags_of([20, 20, 60, 61, 20, 20]) == ["ok", "ok", "spike", "spike", "ok", "ok"]


def test_three_hour_plateau_is_not_a_spike():
    assert flags_of([20, 20, 60, 61, 62, 20, 20]) == ["ok"] * 7


def test_flood_rise_that_stays_up_is_not_a_spike():
    assert flags_of([20, 20, 60, 62, 65, 70, 72]) == ["ok"] * 7


def test_step_below_max_step_is_not_a_spike():
    assert flags_of([20, 29, 20]) == ["ok"] * 3


def test_spike_needs_both_neighbours():
    # First hour has no value before it, last hour none after it.
    assert flags_of([60, 20, 20]) == ["ok"] * 3
    assert flags_of([20, 20, 60]) == ["ok"] * 3


def test_missing_neighbour_prevents_a_spike():
    assert flags_of([20, 60, np.nan, 20]) == ["ok", "ok", "missing", "ok"]


def test_spike_may_go_down():
    assert flags_of([50, 50, 5, 50, 50]) == ["ok", "ok", "spike", "ok", "ok"]


def test_flat_run_of_flat_hours_is_flagged():
    assert flags_of([1, 7, 7, 7, 7, 7, 2]) == ["ok"] + ["flat"] * 5 + ["ok"]


def test_flat_run_shorter_than_flat_hours_is_not_flagged():
    assert flags_of([1, 7, 7, 7, 7, 2]) == ["ok"] * 6


def test_missing_hour_is_not_a_flat_line():
    values = [7, 7, 7, np.nan, 7, 7, 7]
    assert flags_of(values) == ["ok", "ok", "ok", "missing", "ok", "ok", "ok"]


def test_flat_check_can_be_switched_off():
    rules = CleaningRules(0.0, 100.0, 10.0, None)
    assert flags_of([7] * 20, rules) == ["ok"] * 20


def test_missing_input_is_flagged_missing_and_stays_nan():
    result = clean_series(series([1, np.nan, 3]), RULES)
    assert list(result.flags) == ["ok", "missing", "ok"]
    assert result.values.isna().tolist() == [False, True, False]


def test_flagged_values_become_nan_and_input_is_not_changed():
    original = series([20, 20, 60, 20, 150, 20])
    before = original.copy()
    result = clean_series(original, RULES)
    assert result.values.isna().tolist() == [False, False, True, False, True, False]
    assert result.values.index.equals(original.index)
    pd.testing.assert_series_equal(original, before)


def test_range_wins_over_spike_and_flat():
    # 150 is a spike and out of range: counted once, as range.
    assert flags_of([20, 150, 20])[1] == "range"
    # A flat run of out-of-range values is range, not flat.
    assert flags_of([150] * 6) == ["range"] * 6


def test_spike_wins_over_flat():
    rules = CleaningRules(0.0, 100.0, 10.0, 2)
    assert flags_of([20, 20, 60, 60, 20, 20], rules) == [
        "flat",
        "flat",
        "spike",
        "spike",
        "flat",
        "flat",
    ]


def test_checks_use_timestamps_not_row_positions():
    values = series([20, 20, 60, 20, 20, 7, 7, 7, 7, 7, 3])
    shuffled = values.sample(frac=1, random_state=1)
    # Sorting by timestamp restores the grid; the result must be the same.
    expected = clean_series(values, RULES).flags
    actual = clean_series(shuffled.sort_index(), RULES).flags
    pd.testing.assert_series_equal(actual, expected, check_freq=False)
    # The same values on other timestamps give the same flags.
    moved = clean_series(series(values.tolist(), start="2020-06-30 13:00"), RULES).flags
    assert list(moved) == list(expected)


def test_index_with_a_hole_is_rejected():
    s = series([1, 2, 3, 4]).drop(series([1, 2, 3, 4]).index[2])
    with pytest.raises(ValueError, match="regular hourly grid"):
        clean_series(s, RULES)


def test_unsorted_index_is_rejected():
    s = series([1, 2, 3, 4])
    with pytest.raises(ValueError, match="regular hourly grid"):
        clean_series(s.iloc[::-1], RULES)


def test_naive_or_non_utc_index_is_rejected():
    naive = pd.Series([1.0, 2.0], index=pd.date_range("2026-01-01", periods=2, freq="h"))
    with pytest.raises(ValueError, match="timezone-aware"):
        clean_series(naive, RULES)
    zurich = series([1, 2]).tz_convert("Europe/Zurich")
    with pytest.raises(ValueError, match="UTC"):
        clean_series(zurich, RULES)


def test_empty_series_is_fine():
    result = clean_series(series([]), RULES)
    assert len(result.flags) == 0


def test_rules_for_merges_station_override_into_defaults():
    config = CleaningConfig(
        defaults={"W": {"max_step": 0.5, "flat_hours": 48}},
        stations={"1": {"W": {"min": 1, "max": 2, "flat_hours": 10}}},
    )
    assert rules_for(config, "1", "W") == CleaningRules(1, 2, 0.5, 10)


def test_rules_for_without_limits_raises():
    config = CleaningConfig(defaults={"W": {"max_step": 0.5, "flat_hours": 48}}, stations={})
    with pytest.raises(ValueError, match="missing keys"):
        rules_for(config, "1", "W")


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"min": 5, "max": 5, "max_step": 1, "flat_hours": 5}, "min must be below max"),
        ({"min": 0, "max": 5, "max_step": 0, "flat_hours": 5}, "max_step"),
        ({"min": 0, "max": 5, "max_step": 1, "flat_hours": 1}, "flat_hours"),
        ({"min": 0, "max": 5, "max_step": 1, "flat_hours": 5, "x": 1}, "unknown keys"),
    ],
)
def test_invalid_rules_raise(raw, message):
    with pytest.raises(ValueError, match=message):
        build_rules(raw)


def test_unknown_parameter_in_config_raises():
    with pytest.raises(ValueError, match="unknown parameter"):
        parse_cleaning_config({"defaults": {"WT": {}}, "stations": {}})
