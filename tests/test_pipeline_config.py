from datetime import UTC, date, datetime, timedelta

import pytest
import yaml

from flowguard.common.cleaning import PARAMETERS, CleaningRules, rules_for
from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.stations import load_stations

POINT = {"name": "a", "latitude": 46.6, "longitude": 8.6}
CLEANING = {"defaults": {"W": {"max_step": 0.5, "flat_hours": 48}}, "stations": {}}
SPLIT = {
    "train_end": "2026-03-15",
    "val_start": "2026-03-15",
    "val_end": "2026-06-15",
    "test_start": "2026-06-15",
    "test_end": "2026-10-01",
}


def write_config(tmp_path, **changes):
    raw = {
        "bafu_backfill_start": "2011-01-01",
        "weather_start": "2024-08-07",
        "as_of_offset_hours": 3,
        "min_run_age_hours": 7,
        "weather_points": [POINT],
        "cleaning": CLEANING,
        "split": SPLIT,
        **changes,
    }
    path = tmp_path / "pipeline.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_real_config_values():
    config = load_pipeline_config()
    assert config.bafu_backfill_start == date(2011, 1, 1)
    assert config.weather_start == date(2024, 8, 7)
    assert config.as_of_offset == timedelta(hours=3)
    assert config.min_run_age == timedelta(hours=7)


def test_real_config_has_three_distinct_points():
    points = load_pipeline_config().weather_points
    assert [p.name for p in points] == ["andermatt", "meiental", "schaechental"]
    assert len({(p.latitude, p.longitude) for p in points}) == 3


def test_custom_config_loads(tmp_path):
    config = load_pipeline_config(write_config(tmp_path))
    assert config.weather_points[0].latitude == 46.6


def test_duplicate_point_names_are_rejected(tmp_path):
    path = write_config(tmp_path, weather_points=[POINT, POINT])
    with pytest.raises(ValueError, match="Duplicate weather point names"):
        load_pipeline_config(path)


def test_no_points_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="At least one weather point"):
        load_pipeline_config(write_config(tmp_path, weather_points=[]))


def test_latitude_out_of_range_is_rejected(tmp_path):
    path = write_config(tmp_path, weather_points=[{**POINT, "latitude": 91}])
    with pytest.raises(ValueError, match="latitude"):
        load_pipeline_config(path)


def test_longitude_out_of_range_is_rejected(tmp_path):
    path = write_config(tmp_path, weather_points=[{**POINT, "longitude": 181}])
    with pytest.raises(ValueError, match="longitude"):
        load_pipeline_config(path)


@pytest.mark.parametrize("key", ["as_of_offset_hours", "min_run_age_hours"])
def test_non_positive_hours_are_rejected(tmp_path, key):
    with pytest.raises(ValueError, match="must be positive"):
        load_pipeline_config(write_config(tmp_path, **{key: 0}))


def test_first_weather_origin_is_weather_start_plus_72_hours():
    config = load_pipeline_config()
    assert config.first_weather_origin == datetime(2024, 8, 10, 0, tzinfo=UTC)


def test_first_weather_origin_follows_weather_start(tmp_path):
    config = load_pipeline_config(write_config(tmp_path, weather_start="2025-01-01"))
    assert config.first_weather_origin == datetime(2025, 1, 4, 0, tzinfo=UTC)


def test_real_config_has_complete_cleaning_rules_for_every_series():
    config = load_pipeline_config()
    for station in load_stations():
        for parameter in station.parameters:
            if parameter in PARAMETERS:
                assert isinstance(
                    rules_for(config.cleaning, station.number, parameter), CleaningRules
                )


def test_real_config_split_dates():
    split = load_pipeline_config().split
    assert split.train_start is None
    assert split.train_end == datetime(2026, 3, 15, tzinfo=UTC)
    assert split.test_end == datetime(2026, 10, 1, tzinfo=UTC)
    assert split.embargo_hours == 24


def test_invalid_split_in_config_is_rejected(tmp_path):
    path = write_config(tmp_path, split={**SPLIT, "val_start": "2026-01-01"})
    with pytest.raises(ValueError, match="overlap"):
        load_pipeline_config(path)


def test_incomplete_cleaning_rules_are_rejected(tmp_path):
    cleaning = {"defaults": {}, "stations": {"2087": {"W": {"min": 1.0, "max": 2.0}}}}
    with pytest.raises(ValueError, match="missing keys"):
        load_pipeline_config(write_config(tmp_path, cleaning=cleaning))
