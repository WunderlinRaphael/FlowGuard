"""Tests for the feature loader and the train and validation sets (synthetic data)."""

from datetime import timedelta

import pandas as pd
import pyarrow.fs as pafs
import pytest
from training_helpers import FIRST_WEATHER, make_config, make_table, write_store

from flowguard.common import raw_store
from flowguard.common.stations import load_stations
from flowguard.common.weather_features import weather_columns
from flowguard.training.data import (
    LEVEL,
    WEATHER,
    build_datasets,
    feature_names,
    read_feature_table,
)


@pytest.fixture(scope="module")
def config():
    return make_config()


@pytest.fixture(scope="module")
def stations():
    return load_stations()


@pytest.fixture(scope="module")
def table(config, stations):
    return make_table(config, stations)


def test_round_trip_local(tmp_path, table, config, stations):
    write_store(tmp_path, table, config, stations)
    loaded, manifest = read_feature_table(tmp_path, config, stations)
    assert manifest["feature_version"] == config.feature_version
    assert set(loaded["station_id"]) == {"2087", "2491", "2056"}
    # The same rows as the table, up to the end of the validation segment.
    expected = table[table["t"] < config.split.val_end]
    assert len(loaded) == len(expected)
    assert loaded["y_delta_cm"].sum() == pytest.approx(expected["y_delta_cm"].sum())


def test_round_trip_on_a_gs_uri(tmp_path, monkeypatch, table, config, stations):
    def from_uri(uri):
        return pafs.SubTreeFileSystem(str(tmp_path / "bucket"), pafs.LocalFileSystem()), "prefix"

    (tmp_path / "bucket" / "prefix").mkdir(parents=True)
    monkeypatch.setattr(raw_store, "filesystem_from_uri", from_uri)
    write_store("gs://bucket/prefix", table, config, stations)
    loaded, _ = read_feature_table("gs://bucket/prefix", config, stations)
    assert len(loaded) == (table["t"] < config.split.val_end).sum()


def test_loader_never_returns_test_rows(tmp_path, table, config, stations):
    write_store(tmp_path, table, config, stations)
    # The stored June files hold test rows (after 15 June); the loader cuts them off.
    assert (table["t"] >= config.split.val_end).any()
    loaded, _ = read_feature_table(tmp_path, config, stations)
    assert loaded["t"].max() < config.split.val_end


def test_missing_manifest_is_an_error(tmp_path, config, stations):
    with pytest.raises(FileNotFoundError):
        read_feature_table(tmp_path, config, stations)


def test_segments_and_embargo(table, config, stations):
    data = build_datasets(table, config, stations, LEVEL)
    split = config.split
    day = timedelta(hours=24)
    assert data.train.t.max() + day < split.train_end
    assert data.val.t.min() >= split.val_start
    assert data.val.t.max() + day < split.val_end
    # Disjoint: every train row is before every validation row.
    assert data.train.t.max() < data.val.t.min()
    # t just before train_end has its target time outside the segment: embargo.
    assert not (data.train.t == split.train_end - timedelta(hours=1)).any()
    assert not (data.val.t == split.val_end - timedelta(hours=1)).any()


def test_only_rows_with_a_target(table, config, stations):
    data = build_datasets(
        table.assign(y_delta_cm=lambda f: f["y_delta_cm"]), config, stations, LEVEL
    )
    broken = table.copy()
    broken.loc[broken.index[::7], "y_delta_cm"] = float("nan")
    data = build_datasets(broken, config, stations, LEVEL)
    assert data.train.y.notna().all() and data.val.y.notna().all()
    assert len(data.train.y) < len(build_datasets(table, config, stations, LEVEL).train.y)


def test_test_rows_never_influence_the_datasets(table, config, stations):
    reference = build_datasets(table, config, stations, WEATHER)
    poisoned = table.copy()
    in_test = poisoned["t"] >= config.split.test_start
    poisoned.loc[in_test, "y_delta_cm"] = 1e9
    poisoned.loc[in_test, "q_now"] = 1e9
    other = build_datasets(poisoned, config, stations, WEATHER)
    pd.testing.assert_frame_equal(reference.train.X, other.train.X)
    pd.testing.assert_frame_equal(reference.val.X, other.val.X)
    pd.testing.assert_series_equal(reference.val.y, other.val.y)


def test_feature_sets(config, stations):
    level = feature_names(stations, config, LEVEL)
    weather = feature_names(stations, config, WEATHER)
    assert not any(name.startswith("wx_") for name in level)
    assert weather == level + weather_columns(config.weather_points)
    assert len(weather) - len(level) == 37
    for name in ("t", "station_id", "y_delta_cm", "w_release_state", "y_release_state"):
        assert name not in weather
    with pytest.raises(ValueError):
        feature_names(stations, config, "other")


def test_inputs_hold_no_metadata_or_target(table, config, stations):
    data = build_datasets(table, config, stations, WEATHER)
    assert list(data.train.X.columns[:-1]) == feature_names(stations, config, WEATHER)
    assert data.train.X.columns[-1] == "station_id"
    assert isinstance(data.train.X["station_id"].dtype, pd.CategoricalDtype)
    # The same category list in both sets, so LightGBM codes the stations the same way.
    assert list(data.train.X["station_id"].cat.categories) == list(
        data.val.X["station_id"].cat.categories
    )


def test_weather_rows_start_at_the_first_weather_origin(table, config, stations):
    assert config.first_weather_origin == FIRST_WEATHER
    weather = build_datasets(table, config, stations, WEATHER)
    era = build_datasets(table, config, stations, LEVEL, weather_era_only=True)
    level = build_datasets(table, config, stations, LEVEL)
    assert weather.train.t.min() >= FIRST_WEATHER
    # (b) and (c) have the same training rows; (a) has more.
    pd.testing.assert_series_equal(weather.train.t, era.train.t)
    assert len(level.train.y) > len(era.train.y)
    # Validation rows are identical for all three.
    pd.testing.assert_series_equal(weather.val.t, level.val.t)
    pd.testing.assert_series_equal(era.val.y, level.val.y)
