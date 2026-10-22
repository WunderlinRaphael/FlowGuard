"""Pipeline settings (start dates, as-of rule, weather points), loaded from config/pipeline.yaml."""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

# src/flowguard/common/pipeline_config.py -> repository root is three levels above this folder.
CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "pipeline.yaml"


@dataclass(frozen=True)
class WeatherPoint:
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class PipelineConfig:
    bafu_backfill_start: date
    weather_start: date
    as_of_offset: timedelta
    min_run_age: timedelta
    weather_points: tuple[WeatherPoint, ...]


def load_pipeline_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    config = PipelineConfig(
        # YAML reads 2011-01-01 as a date already; str() also accepts a quoted string.
        bafu_backfill_start=date.fromisoformat(str(raw["bafu_backfill_start"])),
        weather_start=date.fromisoformat(str(raw["weather_start"])),
        as_of_offset=timedelta(hours=raw["as_of_offset_hours"]),
        min_run_age=timedelta(hours=raw["min_run_age_hours"]),
        weather_points=tuple(
            WeatherPoint(p["name"], float(p["latitude"]), float(p["longitude"]))
            for p in raw["weather_points"]
        ),
    )
    validate(config)
    return config


def validate(config):
    """Raise ValueError if the pipeline config is inconsistent."""
    if config.as_of_offset <= timedelta(0):
        raise ValueError("as_of_offset_hours must be positive")
    if config.min_run_age <= timedelta(0):
        raise ValueError("min_run_age_hours must be positive")
    if not config.weather_points:
        raise ValueError("At least one weather point is needed")

    names = [p.name for p in config.weather_points]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate weather point names in {names}")
    for p in config.weather_points:
        if not -90 <= p.latitude <= 90:
            raise ValueError(f"Weather point {p.name}: latitude {p.latitude} out of range")
        if not -180 <= p.longitude <= 180:
            raise ValueError(f"Weather point {p.name}: longitude {p.longitude} out of range")
