import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from flowguard.common.split import SplitConfig, assign_segments

# scripts/ is not a package, so the one-off script is loaded from its path.
SPEC = importlib.util.spec_from_file_location(
    "eda_baseline", Path(__file__).resolve().parents[1] / "scripts" / "eda_baseline.py"
)
eda = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eda)


def hours(start, n):
    return pd.date_range(start, periods=n, freq="h", tz="UTC")


def series(values, start="2026-01-01"):
    return pd.Series(values, index=hours(start, len(values)), dtype="float64")


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def split_config():
    return SplitConfig(
        train_start=None,
        train_end=utc(2026, 1, 20),
        val_start=utc(2026, 1, 20),
        val_end=utc(2026, 2, 5),
        test_start=utc(2026, 2, 5),
        test_end=utc(2026, 2, 20),
    )


def test_targets_are_looked_up_by_timestamp():
    w = series(np.arange(100, dtype=float) * 0.01)  # rises 1 cm per hour
    frame = eda.build_targets(w, pd.Series("train", index=w.index))
    assert frame["y_delta_cm"].iloc[0] == pytest.approx(24.0)
    # The last 24 hours have no value 24 h later.
    assert frame["y_delta_cm"].iloc[-24:].isna().all()


def test_a_missing_value_removes_the_rows_that_use_it():
    values = np.arange(100, dtype=float) * 0.01
    values[50] = np.nan
    frame = eda.build_targets(series(values), pd.Series("train", index=hours("2026-01-01", 100)))
    # Row 26 has its target at hour 50, row 50 starts at the gap.
    assert np.isnan(frame["y_delta_cm"].iloc[26])
    assert np.isnan(frame["y_delta_cm"].iloc[50])
    assert not np.isnan(frame["y_delta_cm"].iloc[27])


def test_thresholds_use_only_the_train_segment():
    w = series(np.linspace(0, 1, 101))
    segments = pd.Series(["train"] * 50 + ["val"] * 51, index=w.index)
    w.iloc[60] = 1000.0  # a spike in validation must not move the thresholds
    low, high = eda.band_thresholds(pd.DataFrame({"w": w, "segment": segments}))
    train_w = w.iloc[:50]
    assert low == pytest.approx(train_w.quantile(0.10))
    assert high == pytest.approx(train_w.quantile(0.90))


def test_band_edges_belong_to_low_and_flood():
    frame = pd.DataFrame({"w": [1.0, 2.0, 3.0, 4.0, 5.0, np.nan]}, index=hours("2026-01-01", 6))
    banded = eda.add_band(frame, low=2.0, high=4.0)
    assert list(banded["band"].iloc[:5]) == ["low", "low", "normal", "flood", "flood"]
    assert banded["band"].iloc[5] is None


def test_persistence_error_is_the_absolute_change():
    delta = pd.Series([1.0, -2.0, 5.0, -10.0])
    n, mae, share = eda.persistence_stats(delta)
    assert n == 4
    assert mae == pytest.approx(4.5)
    assert share == pytest.approx(0.5)  # 5 and 10 are above 3 cm


def test_persistence_of_no_rows_is_nan():
    n, mae, share = eda.persistence_stats(pd.Series([], dtype=float))
    assert n == 0 and np.isnan(mae) and np.isnan(share)


def test_episode_gap_of_24_hours_joins_and_25_splits():
    base = pd.Timestamp("2026-01-01", tz="UTC")
    assert eda.count_episodes([base, base + timedelta(hours=24)]) == 1
    assert eda.count_episodes([base, base + timedelta(hours=25)]) == 2
    assert eda.count_episodes([]) == 0
    # Order of the input does not matter.
    assert eda.count_episodes([base + timedelta(hours=100), base]) == 2


def test_cross_correlation_finds_a_six_hour_shift():
    rng = np.random.default_rng(1)
    upstream = pd.Series(rng.normal(size=600), index=hours("2026-01-01", 600))
    downstream = upstream.shift(freq=timedelta(hours=6)).reindex(upstream.index)
    corr = eda.lag_correlations(upstream, downstream)
    assert corr.idxmax() == 6
    assert corr[6] == pytest.approx(1.0)


def test_cross_correlation_of_short_input_is_nan_not_an_error():
    short = pd.Series([1.0, 2.0, 3.0], index=hours("2026-01-01", 3))
    assert eda.lag_correlations(short, short).isna().all()


def test_diff_over_a_window_uses_timestamps():
    s = series([0.0, 1.0, 3.0, 6.0, 10.0])
    assert list(eda.hourly_diff(s).iloc[1:]) == [1.0, 2.0, 3.0, 4.0]
    assert list(eda.hourly_diff(s, hours=2).iloc[2:]) == [3.0, 5.0, 7.0]
    assert eda.hourly_diff(s).iloc[0] != eda.hourly_diff(s).iloc[0]  # first value is NaN


def test_only_train_keeps_train_hours():
    s = series(np.arange(10, dtype=float))
    segments = pd.Series(["train"] * 4 + ["val"] * 6, index=s.index)
    kept = eda.only_train(s, segments)
    assert kept.notna().sum() == 4


def test_empty_release_state_counts_missing_and_empty_strings():
    state = pd.Series([None, "", "released", np.nan], dtype="object")
    assert list(eda.empty_release(state)) == [True, True, False, True]


def test_thin_bands_only_look_at_validation_and_test():
    events = pd.DataFrame(
        {
            "segment": ["train", "val", "test", "val"],
            "rows": [5, 500, 40, 99],
        }
    )
    assert list(eda.thin_bands(events)["rows"]) == [40, 99]


def test_sentence_ignores_thin_bands():
    persistence = pd.DataFrame(
        {
            "station": ["s"] * 4,
            "segment": ["val"] * 4,
            "band": ["all", "low", "normal", "flood"],
            "rows": [1000, 500, 450, 50],
            "mae_cm": [2.0, 1.0, 2.5, 40.0],
            "share_over_3cm": [0.1, 0.0, 0.1, 1.0],
        }
    )
    (line,) = eda.persistence_sentences(persistence)
    assert "2 of 2 bands" in line and "3 cm is easy" in line


def test_analysis_runs_on_a_small_synthetic_grid(tmp_path):
    # Real station numbers (their cleaning limits apply), a daily wave of W and Q.
    from flowguard.common.pipeline_config import load_pipeline_config
    from flowguard.common.stations import load_stations
    from flowguard.features.hourly_grid import build_hourly_grid

    cfg = load_pipeline_config()
    cfg = replace(cfg, split=split_config())
    t = hours("2026-01-01", 24 * 60)
    base = {"2087": 1427.0, "2491": 485.8, "2056": 438.0, "2299": 1020.0}
    level_q = {"2087": 20, "2491": 8, "2056": 90, "2299": 5, "2492": 0.5, "2649": 2}
    rows = []
    for i, ts in enumerate(t):
        wave = np.sin(i / 30) + 0.3 * np.sin(i / 7)
        for station, q in level_q.items():
            rows.append(row(station, "Q", ts, q * (1.5 + wave / 2)))
            if station in base:
                rows.append(row(station, "W", ts, base[station] + 0.2 * wave))
    grid = build_hourly_grid(rows).grid
    a = eda.analyse(grid, cfg, load_stations())

    assert set(a.persistence["segment"]) == {"train", "val", "test"}
    assert not a.persistence["rows"].lt(0).any()
    labels = assign_segments(t, cfg.split)
    expected_val = int((labels == "val").sum())
    # Hours at the end of the data have no target, the segment sizes may only be smaller.
    val_all = a.persistence[(a.persistence["segment"] == "val") & (a.persistence["band"] == "all")]
    assert (val_all["rows"] <= expected_val).all()
    assert len(a.travel) >= 1

    out = tmp_path / "notes.md"
    text = eda.render(a, utc(2026, 3, 1), "test-store", cfg, [], len(rows))
    out.write_text(text, encoding="utf-8")
    assert "## Persistence baseline" in text and "## Travel time" in text


def row(station, parameter, ts, value):
    return {
        "station": station,
        "timestamp": ts.to_pydatetime(),
        "parameter": parameter,
        "value": float(value),
        "unit": "x",
        "release_state": None,
        "fetched_at": datetime(2026, 3, 1, tzinfo=UTC),
    }
