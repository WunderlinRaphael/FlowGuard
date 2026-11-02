"""The feature table: BAFU features and weather features joined on station_id and t.

One function builds the whole table from the hourly grid and the stored weather rows, so
training and inference call the same code. It reads no files and no clock.

Columns, in this order: metadata (station_id, t, release states), the BAFU features and
calendar features, the weather features, the target. The same columns for every station.
"""

import pandas as pd

from flowguard.common.cleaning import PARAMETERS
from flowguard.common.features import METADATA, TARGET, build_features, feature_columns
from flowguard.common.stations import targets
from flowguard.common.weather_features import build_weather_features, weather_columns

STRING_COLUMNS = ("station_id", "w_release_state", "y_release_state")


def feature_table_columns(stations, config):
    """Column names of the stored table, in order."""
    return [
        *METADATA,
        *feature_columns(stations),
        *weather_columns(config.weather_points),
        TARGET,
    ]


def build_feature_table(grid, weather_rows, stations, config):
    """Feature table for all target stations.

    Each station runs from the first to the last delivered hour of its own W or Q series. The
    newest hours have no target and stay in. Weather does not depend on the station, so it is
    computed once per hour t and joined on t; rows before config.first_weather_origin have
    missing weather columns. No split is applied.
    """
    columns = feature_table_columns(stations, config)
    table = build_features(grid, stations, config.cleaning)
    if table.empty:
        return pd.DataFrame(columns=columns)

    table = _trim_to_station_spans(table, grid, stations)
    if table.empty:
        return pd.DataFrame(columns=columns)

    origins = pd.DatetimeIndex(sorted(table["t"].unique()))
    weather = build_weather_features(weather_rows, origins, config)
    table = table.merge(weather, on="t", how="left", validate="many_to_one")
    return table[columns].sort_values(["station_id", "t"], kind="stable").reset_index(drop=True)


def _trim_to_station_spans(table, grid, stations):
    own = grid[grid["parameter"].isin(PARAMETERS) & ~grid["is_missing"]]
    kept = []
    for station in targets(stations):
        series = own[own["station"] == station.number]["timestamp"]
        rows = table[table["station_id"] == station.number]
        if series.empty:
            continue
        kept.append(rows[(rows["t"] >= series.min()) & (rows["t"] <= series.max())])
    return pd.concat(kept, ignore_index=True) if kept else table.iloc[0:0]
