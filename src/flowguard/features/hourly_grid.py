"""Regular hourly UTC grid per station and parameter, built from raw BAFU rows.

Hours without a value stay in the grid as missing values (NaN, is_missing=True).
Nothing is filled or interpolated. release_state and fetched_at are kept as delivered.
"""

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from flowguard.features.bafu_client import check_utc

COLUMNS = [
    "station",
    "parameter",
    "timestamp",
    "value",
    "unit",
    "release_state",
    "fetched_at",
    "is_missing",
]
SERIES = ["station", "parameter"]


@dataclass(frozen=True)
class GridResult:
    grid: pd.DataFrame
    # Number of rows dropped because the same station, parameter and timestamp came twice.
    duplicates: int


def check_hour(ts):
    """Raise ValueError unless ts is a UTC datetime exactly on the hour."""
    check_utc(ts)
    if ts.minute or ts.second or ts.microsecond:
        raise ValueError(f"Expected a timestamp on the hour, got {ts!r}")


def empty_grid():
    return pd.DataFrame(
        {
            "station": pd.Series(dtype="object"),
            "parameter": pd.Series(dtype="object"),
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "value": pd.Series(dtype="float64"),
            "unit": pd.Series(dtype="object"),
            "release_state": pd.Series(dtype="object"),
            "fetched_at": pd.Series(dtype="datetime64[ns, UTC]"),
            "is_missing": pd.Series(dtype="bool"),
        }
    )[COLUMNS]


def build_hourly_grid(rows, start=None, end=None):
    """Turn raw rows into a regular hourly grid per station and parameter.

    Without start and end, each series spans its first to its last delivered hour, so the
    hours after the newest value (publication delay) are not counted as gaps. With start and
    end (UTC, end exclusive) every series spans exactly that window.
    """
    rows = list(rows)
    for row in rows:
        check_hour(row["timestamp"])
    for bound in (start, end):
        if bound is not None:
            check_hour(bound)
    if not rows:
        return GridResult(empty_grid(), 0)

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.as_unit("ns")
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True).dt.as_unit("ns")
    df["value"] = pd.to_numeric(df["value"]).astype("float64")

    # Same station, parameter and hour twice (for example overlapping fetches): the row
    # fetched last wins. The sort is stable, so equal fetched_at keeps the later input row.
    before = len(df)
    df = df.sort_values("fetched_at", kind="stable")
    df = df.drop_duplicates(SERIES + ["timestamp"], keep="last")
    duplicates = before - len(df)

    parts = []
    for (station, parameter), series in df.groupby(SERIES, sort=True):
        parts.append(_series_grid(station, parameter, series, start, end))
    grid = pd.concat(parts, ignore_index=True)[COLUMNS]
    return GridResult(grid, duplicates)


def _series_grid(station, parameter, series, start, end):
    units = series["unit"].dropna().unique()
    if len(units) > 1:
        raise ValueError(f"Station {station} {parameter}: several units {list(units)}")

    first = pd.Timestamp(start) if start is not None else series["timestamp"].min()
    stop = pd.Timestamp(end) if end is not None else series["timestamp"].max() + timedelta(hours=1)
    index = pd.date_range(first, stop, freq="h", inclusive="left", name="timestamp", unit="ns")

    out = series.set_index("timestamp").reindex(index).reset_index()
    out["station"] = station
    out["parameter"] = parameter
    # The unit belongs to the series, not to a single hour, so it is also set on missing hours.
    out["unit"] = units[0] if len(units) else None
    out["is_missing"] = out["value"].isna()
    return out
