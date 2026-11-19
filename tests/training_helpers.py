"""A small synthetic feature table for the training tests (no network, no real data).

The table has the real column layout. The target depends on one feature (w_d6h), so a model
can learn it, and persistence (change 0) is clearly worse.
"""

from dataclasses import replace
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from flowguard.common.feature_table import feature_table_columns
from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.raw_store import RawStore
from flowguard.common.stations import targets
from flowguard.features.feature_store import FeatureStore, feature_schema, to_arrow

START = datetime(2026, 2, 1, tzinfo=UTC)
END = datetime(2026, 7, 1, tzinfo=UTC)
# Weather exists from this origin on (config weather_start + 72 h).
FIRST_WEATHER = datetime(2026, 2, 13, tzinfo=UTC)
SIGNAL = "w_d6h"


def make_config():
    """The real config, with the weather start moved into the fixture period."""
    config = load_pipeline_config()
    return replace(config, weather_start=date(2026, 2, 10))


def make_table(config, stations, seed=0):
    """Hourly rows for the three targets from START to END; target = 3 * w_d6h + noise."""
    rng = np.random.default_rng(seed)
    columns = feature_table_columns(stations, config)
    times = pd.date_range(START, END, freq="h", inclusive="left")
    frames = []
    for station in targets(stations):
        frame = pd.DataFrame(np.nan, index=range(len(times)), columns=columns)
        frame["station_id"] = station.number
        frame["t"] = times
        frame["w_release_state"] = None
        frame["y_release_state"] = None
        frame[SIGNAL] = rng.normal(size=len(times))
        frame["q_now"] = rng.normal(size=len(times))
        frame["y_delta_cm"] = 3 * frame[SIGNAL] + rng.normal(scale=0.1, size=len(times))
        frame["doy_sin"] = np.sin(times.dayofyear * 2 * np.pi / 365.25)
        frame["doy_cos"] = np.cos(times.dayofyear * 2 * np.pi / 365.25)
        # Weather only from the first weather origin, as in the real table.
        has_weather = times >= FIRST_WEATHER
        for name in [c for c in columns if c.startswith("wx_")]:
            frame.loc[has_weather, name] = rng.normal(size=int(has_weather.sum()))
        frames.append(frame)
    table = pd.concat(frames, ignore_index=True)
    # The newest 24 hours have no target, as in the real table.
    table.loc[table["t"] >= END - pd.Timedelta(hours=24), "y_delta_cm"] = np.nan
    return table[columns]


def write_store(base, table, config, stations):
    """Write the table month by month like features/build.py and add a manifest.

    base is a local folder or a gs:// path (with the fake file system of the test).
    """
    raw = RawStore(base)
    store = FeatureStore(raw.fs, raw.root, config.feature_version)
    schema = feature_schema(stations, config)
    months = table["t"].dt.strftime("%Y-%m")
    for (station, month), part in table.groupby(["station_id", months]):
        store.write_month(station, month, to_arrow(part.reset_index(drop=True), schema))
    store.write_manifest(
        {
            "feature_version": config.feature_version,
            "built_at": "2026-07-02T00:00:00Z",
            "code_version": "test",
            "newest_fetched_at": {
                "bafu": "2026-07-01T00:00:00Z",
                "weather": "2026-07-01T00:00:00Z",
            },
        }
    )
    return store
