"""Lag and rolling features per target station and origin hour t.

One row per target station and hour t, the same columns in the same order for every station.
A feature at t uses only values stamped t or earlier (BAFU stamps mark the start of the hour).
Lags and windows are looked up by timestamp on the regular hourly grid, so a missing hour gives
a missing value and never a shifted one. Training and inference both call build_features.
The split is not part of the table, and there are no weather columns here.
"""

import math

import numpy as np
import pandas as pd

from flowguard.common.cleaning import PARAMETERS, clean_series, rules_for
from flowguard.common.stations import targets

HORIZON = pd.Timedelta(hours=24)
# Differences to the value at t minus this much.
LAGS = {"d1h": "1h", "d6h": "6h", "d24h": "24h", "d72h": "72h", "d7d": "7D"}
# Rolling windows in hours, ending at t (t itself is part of the window).
WINDOWS = {"roll24": 24, "roll72": 72}
STATS = ("mean", "std", "min", "max")
# A rolling value needs at least this share of the hours present.
MIN_SHARE = 0.75
METADATA = ["station_id", "t", "w_release_state", "y_release_state"]
CALENDAR = ["doy_sin", "doy_cos"]
TARGET = "y_delta_cm"


def series_specs(stations):
    """(prefix, slot, parameter) for every series the features are built from.

    slot is "own", "up_main", "up_trib" or the number of a diversion station. The diversion
    stations are the union over all targets, so every station gets the same columns.
    """
    div = sorted({n for s in targets(stations) for n in s.div_q})
    return [
        ("w", "own", "W"),
        ("q", "own", "Q"),
        ("up_main_w", "up_main", "W"),
        ("up_main_q", "up_main", "Q"),
        ("up_trib_w", "up_trib", "W"),
        ("up_trib_q", "up_trib", "Q"),
        *[(f"div_q_{n}", n, "Q") for n in div],
    ]


def series_columns(prefix):
    lags = [f"{prefix}_{lag}" for lag in LAGS]
    rolling = [f"{prefix}_{w}_{stat}" for w in WINDOWS for stat in STATS]
    return [f"{prefix}_now", *lags, *rolling]


def feature_columns(stations):
    """Feature names in table order (without metadata and target)."""
    names = [c for prefix, _, _ in series_specs(stations) for c in series_columns(prefix)]
    return names + CALENDAR


def table_columns(stations):
    return [*METADATA, *feature_columns(stations), TARGET]


def clean_grid(grid, cleaning, causal=False):
    """Clean every Q and W series of the grid with the rules of its station and parameter.

    causal is passed to clean_series. The default (False) is what the run report counts;
    build_features uses causal=True for the features.

    Returns (series, flags), both keyed by (station, parameter). series are cleaned values in
    the unit of the grid (flagged hours are NaN), all on one common hourly index. flags keep
    the own index of each series. Other parameters (WT) are not used.
    """
    series, flags = {}, {}
    for (station, parameter), part in grid.groupby(["station", "parameter"], sort=True):
        if parameter not in PARAMETERS:
            continue
        raw = part.set_index("timestamp")["value"]
        result = clean_series(raw, rules_for(cleaning, station, parameter), causal=causal)
        series[(station, parameter)] = result.values
        flags[(station, parameter)] = result.flags
    if series:
        index = _common_index(series.values())
        series = {key: s.reindex(index) for key, s in series.items()}
    return series, flags


def build_features(grid, stations, cleaning):
    """Feature table for all target stations from the hourly grid (long format)."""
    columns = table_columns(stations)
    # Features only see checks that look backwards (causal), so the value at t is the same in
    # training and live. The label is checked with the whole series, see _station_table.
    cleaned, _ = clean_grid(grid, cleaning, causal=True)
    if not cleaned:
        return pd.DataFrame(columns=columns)
    labels, _ = clean_grid(grid, cleaning, causal=False)
    index = next(iter(cleaned.values())).index
    release = _release_states(grid, index)

    frames = [
        _station_table(station, stations, cleaned, labels, release, index)
        for station in sorted(targets(stations), key=lambda s: s.number)
    ]
    return pd.concat(frames, ignore_index=True)[columns]


def _station_table(station, stations, cleaned, labels, release, index):
    parts = {
        "station_id": pd.Series(station.number, index=index),
        "t": pd.Series(index, index=index),
    }
    w_release = release.get(station.number, pd.Series(None, index=index, dtype="object"))
    parts["w_release_state"] = w_release
    parts["y_release_state"] = w_release.shift(freq=-HORIZON).reindex(index)

    for prefix, slot, parameter in series_specs(stations):
        number = _slot_station(station, slot)
        s = cleaned.get((number, parameter)) if number is not None else None
        if s is None:
            s = pd.Series(np.nan, index=index)
        elif parameter == "W":
            # Levels arrive in m a.s.l.; features and target are in cm.
            s = s * 100
        parts.update(_series_features(s, prefix))

    doy = index.dayofyear.to_numpy()
    angle = 2 * math.pi * doy / 365.25
    parts["doy_sin"] = pd.Series(np.sin(angle), index=index)
    parts["doy_cos"] = pd.Series(np.cos(angle), index=index)

    # W(t) is the value the features see (the same as w_now). W(t+24h) is the label and comes
    # from the non-causal check, so a spike at t+24h gives a missing target instead of a wrong one.
    w_now = cleaned.get((station.number, "W"))
    w_label = labels.get((station.number, "W"))
    if w_now is None or w_label is None:
        parts[TARGET] = pd.Series(np.nan, index=index)
    else:
        w_later = w_label.shift(freq=-HORIZON).reindex(index)
        parts[TARGET] = (w_later - w_now) * 100

    frame = pd.DataFrame(parts, index=index)
    frame.index.name = None
    return frame.reset_index(drop=True)


def _slot_station(station, slot):
    if slot == "own":
        return station.number
    if slot == "up_main":
        return station.up_main
    if slot == "up_trib":
        return station.up_trib
    # A diversion station only counts for the stations that list it.
    return slot if slot in station.div_q else None


def _series_features(s, prefix):
    """Current value, differences to earlier hours and rolling statistics of one series."""
    out = {f"{prefix}_now": s}
    for name, lag in LAGS.items():
        # shift(freq=...) moves the timestamps, so the lag is looked up by time, not position.
        earlier = s.shift(freq=pd.Timedelta(lag)).reindex(s.index)
        out[f"{prefix}_{name}"] = s - earlier
    for name, hours in WINDOWS.items():
        # The time-based window (t - hours, t] holds `hours` grid hours.
        window = s.rolling(f"{hours}h", min_periods=math.ceil(MIN_SHARE * hours))
        for stat in STATS:
            out[f"{prefix}_{name}_{stat}"] = getattr(window, stat)()
    return out


def _release_states(grid, index):
    """release_state of the W series per station, on the common index."""
    out = {}
    for station, part in grid[grid["parameter"] == "W"].groupby("station"):
        out[station] = part.set_index("timestamp")["release_state"].reindex(index)
    return out


def _common_index(series):
    series = list(series)
    first = min(s.index[0] for s in series)
    last = max(s.index[-1] for s in series)
    return pd.date_range(first, last, freq="h", name="timestamp")
