import importlib.util
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from flowguard.common.evaluation import EvaluationConfig
from flowguard.training import bands

# scripts/ is not a package, so the one-off script is loaded from its path.
SPEC = importlib.util.spec_from_file_location(
    "eda_baseline", Path(__file__).resolve().parents[1] / "scripts" / "eda_baseline.py"
)
eda = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eda)

RULES = EvaluationConfig(min_episodes_per_band=5, episode_gap_hours=24, pool_thin_bands=True)
START = pd.Timestamp("2026-01-01", tz="UTC")
TRAIN_HOURS, VAL_HOURS, TEST_HOURS = 1000, 600, 400
VAL_START = TRAIN_HOURS
TEST_START = TRAIN_HOURS + VAL_HOURS
# Flood blocks are 3 hours long and 48 h apart, so each one is its own episode.
BLOCK = 3
SPACING = 48


def make_frame(val_floods=(), test_floods=(), val_lows=()):
    """One station: train W between 2 and 4, then quiet 3.0 with flood (10.0) or low (-10.0) blocks.

    val_floods / test_floods / val_lows: block numbers 0, 1, 2 ... inside that segment.
    """
    n = TRAIN_HOURS + VAL_HOURS + TEST_HOURS
    index = pd.date_range(START, periods=n, freq="h")
    w = np.full(n, 3.0)
    w[:TRAIN_HOURS] = np.random.default_rng(0).uniform(2, 4, TRAIN_HOURS)
    for first, blocks, value in (
        (VAL_START, val_floods, 10.0),
        (TEST_START, test_floods, 10.0),
        (VAL_START, val_lows, -10.0),
    ):
        for k in blocks:
            w[first + k * SPACING : first + k * SPACING + BLOCK] = value
    segment = ["train"] * TRAIN_HOURS + ["val"] * VAL_HOURS + ["test"] * TEST_HOURS
    return pd.DataFrame(
        {"w": w, "y_delta_cm": 0.0, "segment": segment},
        index=index,
    )


def row(report, station, segment, band):
    found = report[
        (report["station"] == station) & (report["segment"] == segment) & (report["band"] == band)
    ]
    assert len(found) == 1
    return found.iloc[0]


def test_thresholds_use_only_the_train_segment():
    frame = make_frame(val_floods=[0])  # the flood block in validation must not move them
    low, high = bands.band_thresholds(frame["w"], frame["segment"])
    train_w = frame["w"][:TRAIN_HOURS]
    assert low == pytest.approx(train_w.quantile(0.10))
    assert high == pytest.approx(train_w.quantile(0.90))


def test_band_edges_belong_to_low_and_flood():
    w = pd.Series(
        [1.0, 2.0, 3.0, 4.0, 5.0, np.nan], index=pd.date_range(START, periods=6, freq="h")
    )
    band = bands.assign_band(w, low=2.0, high=4.0)
    assert list(band.iloc[:5]) == ["low", "low", "normal", "flood", "flood"]
    assert band.iloc[5] is None


def test_episode_gap_of_24_hours_joins_and_25_splits():
    gap = timedelta(hours=24)
    assert bands.count_episodes([START, START + timedelta(hours=24)], gap) == 1
    assert bands.count_episodes([START, START + timedelta(hours=25)], gap) == 2
    assert bands.count_episodes([], gap) == 0
    # Order of the input does not matter.
    assert bands.count_episodes([START + timedelta(hours=100), START], gap) == 2


def test_enough_own_episodes_make_a_band_evaluable():
    report = bands.band_report({"A": make_frame(val_floods=range(5))}, RULES)
    flood = row(report, "A", "val", "flood")
    assert (flood["rows"], flood["episodes"]) == (5 * BLOCK, 5)
    assert flood["evaluable"] and not flood["pooled"]


def test_four_episodes_are_not_enough():
    report = bands.band_report({"A": make_frame(val_floods=range(4))}, RULES)
    flood = row(report, "A", "val", "flood")
    assert flood["episodes"] == 4
    assert not flood["evaluable"] and not flood["pooled"]


def test_thin_bands_are_pooled_over_the_stations():
    # Different weeks at each station: 2 + 2 + 1 separate events, 5 together.
    frames = {
        "A": make_frame(val_floods=[0, 3]),
        "B": make_frame(val_floods=[1, 4]),
        "C": make_frame(val_floods=[2]),
    }
    report = bands.band_report(frames, RULES)
    for station in frames:
        flood = row(report, station, "val", "flood")
        assert flood["pooled_episodes"] == 5
        assert flood["evaluable"] and flood["pooled"]


def test_pooling_counts_one_flood_wave_once():
    # The same three waves at all stations: 9 if summed, 3 as a union, so not evaluable.
    frames = {s: make_frame(val_floods=[0, 1, 2]) for s in "ABC"}
    report = bands.band_report(frames, RULES)
    flood = row(report, "A", "val", "flood")
    assert flood["episodes"] == 3 and flood["pooled_episodes"] == 3
    assert not flood["evaluable"]


def test_pooling_can_be_switched_off():
    frames = {"A": make_frame(val_floods=[0, 3]), "B": make_frame(val_floods=[1, 4, 6])}
    report = bands.band_report(frames, replace(RULES, pool_thin_bands=False))
    assert not row(report, "A", "val", "flood")["evaluable"]
    assert not row(report, "B", "val", "flood")["evaluable"]
    # The pooled count is still reported.
    assert row(report, "A", "val", "flood")["pooled_episodes"] == 5


def test_a_band_with_no_rows_has_zero_episodes():
    report = bands.band_report({"A": make_frame()}, RULES)
    low = row(report, "A", "test", "low")
    assert (low["rows"], low["episodes"], low["evaluable"]) == (0, 0, False)


def test_rows_without_a_target_are_not_counted():
    frame = make_frame(val_floods=range(5))
    # No target for the last flood block: only four episodes are left.
    last = VAL_START + 4 * SPACING
    frame.iloc[last : last + BLOCK, frame.columns.get_loc("y_delta_cm")] = np.nan
    flood = row(bands.band_report({"A": frame}, RULES), "A", "val", "flood")
    assert flood["episodes"] == 4 and not flood["evaluable"]


def test_normal_band_with_one_long_episode_is_evaluable_and_not_pooled():
    # Quiet validation segment: all rows are normal, in a single episode.
    report = bands.band_report({"A": make_frame()}, RULES)
    normal = row(report, "A", "val", "normal")
    assert normal["episodes"] == 1 and normal["rows"] > 100
    assert normal["evaluable"] and not normal["pooled"]


def test_normal_band_without_rows_is_not_evaluable():
    frame = make_frame()
    # No target in validation: the band has no rows.
    frame.loc[frame["segment"] == "val", "y_delta_cm"] = np.nan
    normal = row(bands.band_report({"A": frame}, RULES), "A", "val", "normal")
    assert (normal["rows"], normal["episodes"], normal["evaluable"]) == (0, 0, False)


def test_report_has_one_row_per_station_segment_and_band():
    report = bands.band_report({"A": make_frame(), "B": make_frame()}, RULES)
    assert len(report) == 2 * 3 * 3
    assert list(report.columns) == bands.REPORT_COLUMNS


def test_counts_equal_the_eda_script():
    frames = {
        "A": make_frame(val_floods=range(5), test_floods=[0, 1], val_lows=[6, 8]),
        "B": make_frame(val_floods=[1, 3]),
    }
    # Real targets (W 24 h later, missing for the last 24 h), built by the script's own function.
    frames = {s: eda.build_targets(f["w"], f["segment"]) for s, f in frames.items()}
    report = bands.band_report(frames, RULES)

    eda_frames = {}
    for station, frame in frames.items():
        low, high = eda.band_thresholds(frame)
        # Same band limits and bands from both code paths.
        assert bands.band_thresholds(frame["w"], frame["segment"]) == (low, high)
        eda_frames[station] = eda.add_band(frame, low, high)
        assert list(eda_frames[station]["band"]) == list(bands.assign_band(frame["w"], low, high))
    expected = eda.band_events(eda_frames, {s: s for s in eda_frames})
    # The script labels stations "number name"; station_no is the plain key.
    merged = report.merge(
        expected.drop(columns="station"),
        left_on=["station", "segment", "band"],
        right_on=["station_no", "segment", "band"],
    )
    assert len(merged) == len(report) == len(expected)
    assert (merged["rows_x"] == merged["rows_y"]).all()
    assert (merged["episodes_x"] == merged["episodes_y"]).all()
