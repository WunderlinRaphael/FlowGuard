"""Gap report for the hourly grid: every run of missing hours per station and parameter."""

from dataclasses import dataclass

import pandas as pd

from flowguard.features.hourly_grid import SERIES


@dataclass(frozen=True)
class GapReport:
    # One row per gap: station, parameter, start, end (last missing hour), hours, all_stations.
    gaps: pd.DataFrame
    # One row per series: hours_total, missing_hours, share_missing, n_gaps, longest_gap_hours.
    summary: pd.DataFrame
    # Hours that are missing for all stations at once (a hole on the BAFU side).
    all_station_hours: list
    duplicates: int


def all_stations_missing(grid):
    """Hours inside the span of at least two stations where every one of them has no value.

    A station is missing when all its parameters are missing. A station whose series already
    ended (like 2299, which is days late) does not cover the hour and is ignored.
    """
    per_station = grid.groupby(["station", "timestamp"])["is_missing"].all().reset_index()
    per_hour = per_station.groupby("timestamp")["is_missing"].agg(["size", "sum"])
    common = (per_hour["size"] >= 2) & (per_hour["sum"] == per_hour["size"])
    return list(per_hour.index[common])


def gap_report(grid, duplicates=0):
    hours = all_stations_missing(grid)
    flagged = grid.assign(all_stations=grid["timestamp"].isin(hours))

    gaps = []
    for (station, parameter), series in flagged.groupby(SERIES, sort=True):
        missing = series["is_missing"]
        # A new run starts whenever the missing flag changes.
        run = (missing != missing.shift()).cumsum()
        found = (
            series[missing]
            .groupby(run[missing])
            .agg(
                start=("timestamp", "min"),
                end=("timestamp", "max"),
                hours=("timestamp", "size"),
                all_stations=("all_stations", "all"),
            )
        )
        found.insert(0, "parameter", parameter)
        found.insert(0, "station", station)
        gaps.append(found)
    gaps = pd.concat(gaps, ignore_index=True) if gaps else _empty_gaps()

    summary = grid.groupby(SERIES).agg(
        hours_total=("is_missing", "size"), missing_hours=("is_missing", "sum")
    )
    summary["share_missing"] = summary["missing_hours"] / summary["hours_total"]
    by_series = gaps.groupby(SERIES)["hours"]
    summary["n_gaps"] = by_series.size().reindex(summary.index, fill_value=0)
    summary["longest_gap_hours"] = by_series.max().reindex(summary.index, fill_value=0)
    return GapReport(gaps, summary.reset_index(), hours, duplicates)


def _empty_gaps():
    return pd.DataFrame(columns=["station", "parameter", "start", "end", "hours", "all_stations"])


def format_report(report):
    """Plain text for the run log."""
    lines = [f"Duplicate rows dropped: {report.duplicates}"]
    for s in report.summary.itertuples():
        lines.append(
            f"{s.station} {s.parameter}: {s.n_gaps} gaps, longest {s.longest_gap_hours} h, "
            f"{s.share_missing:.2%} missing of {s.hours_total} h"
        )
    for g in report.gaps.itertuples():
        note = " (all stations)" if g.all_stations else ""
        lines.append(
            f"  {g.station} {g.parameter}: {g.start:%Y-%m-%d %H:%M} to "
            f"{g.end:%Y-%m-%d %H:%M} UTC, {g.hours} h{note}"
        )
    lines.append(f"Hours missing for all stations: {len(report.all_station_hours)}")
    return "\n".join(lines)
