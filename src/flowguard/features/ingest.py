"""Hourly ingest: the last 48 hours of BAFU data and the newest weather runs.

    uv run python -m flowguard.features.ingest

Safe to run again at any time: only BAFU rows that are new or changed since the last run
and weather runs that are not stored yet are written. Nothing is overwritten.
"""

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

import pandas as pd

from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.raw_store import RawStore
from flowguard.common.stations import load_stations
from flowguard.features.backfill import RUN_STEP, floor_hour, latest_run, run_unit
from flowguard.features.bafu_client import BafuClient, BafuError
from flowguard.features.gap_report import gap_report
from flowguard.features.hourly_grid import build_hourly_grid
from flowguard.features.weather_client import RunNotAvailableError, WeatherClient, WeatherError

log = logging.getLogger(__name__)

# BAFU data of the last 48 hours is fetched again every hour, to catch late and corrected values.
BAFU_WINDOW = timedelta(hours=48)
# Runs missed during an outage are caught up this far back. Older gaps are the backfill's job.
WEATHER_LOOKBACK = timedelta(hours=48)


def version(value, release_state):
    """What counts as 'the same row' for BAFU: the value and the release state."""
    value = None if pd.isna(value) else float(value)
    release_state = None if pd.isna(release_state) else release_state
    return value, release_state


def new_or_changed(rows, stored):
    """Keep the fetched rows whose value or release state differs from the newest stored version."""
    newest = {}
    for r in stored.sort_values("fetched_at", kind="stable").itertuples():
        newest[(r.station, r.parameter, pd.Timestamp(r.timestamp))] = version(
            r.value, r.release_state
        )
    # A row without a stored version is new, even if its value is empty.
    unseen = object()
    return [
        r
        for r in rows
        if newest.get((r["station"], r["parameter"], pd.Timestamp(r["timestamp"])), unseen)
        != version(r["value"], r["release_state"])
    ]


def ingest_bafu(store, client, stations, now):
    start = floor_hour(now) - BAFU_WINDOW
    rows = client.fetch_stations(stations, start, floor_hour(now))
    fresh = new_or_changed(rows, store.read_bafu(from_timestamp=start))
    if fresh:
        store.write_bafu(fresh, f"hourly-{now:%Y%m%dT%H%M}")
    log.info("BAFU: %d rows fetched, %d new or changed", len(rows), len(fresh))


def ingest_weather(store, client, config, now):
    latest = latest_run(now, config)
    candidates = [latest - i * RUN_STEP for i in reversed(range(WEATHER_LOOKBACK // RUN_STEP + 1))]
    stored = store.units("weather")
    written = 0
    for run_start in candidates:
        if run_unit(run_start) in stored:
            continue
        try:
            rows = client.fetch_run(config.weather_points, run_start)
        except RunNotAvailableError:
            # Not published yet; the next hourly run tries again.
            log.info("Weather: run %s not available yet", run_start)
            continue
        store.write_weather(rows, run_unit(run_start), run_start)
        written += 1
    log.info("Weather: %d new runs (latest allowed run %s)", written, latest)


def short_gap_report(store, config, now):
    """The gaps of the last 48 hours, up to the last hour BAFU can have delivered."""
    start = floor_hour(now) - BAFU_WINDOW
    # The last complete hour is t = now - as_of_offset; publication delay is not a gap.
    end = floor_hour(now) - config.as_of_offset + timedelta(hours=1)
    stored = store.read_bafu(from_timestamp=start)
    result = build_hourly_grid(stored.to_dict("records"), start, end)
    if result.grid.empty:
        return "Gap report: no BAFU data in the last 48 hours"
    report = gap_report(result.grid, result.duplicates)
    lines = [f"Gap report {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC (end exclusive)"]
    for g in report.gaps.itertuples():
        note = " (all stations)" if g.all_stations else ""
        lines.append(
            f"  {g.station} {g.parameter}: {g.start:%m-%d %H:%M} to {g.end:%m-%d %H:%M}, "
            f"{g.hours} h{note}"
        )
    if report.gaps.empty:
        lines.append("  no gaps")
    return "\n".join(lines)


def ingest(store, bafu, weather, stations, config, now):
    """Run one hourly ingest. Returns True if no fetch failed."""
    ok = True
    # A failed source does not stop the other one.
    try:
        ingest_bafu(store, bafu, stations, now)
    except BafuError as error:
        log.error("BAFU fetch failed: %s", error)
        ok = False
    try:
        ingest_weather(store, weather, config, now)
    except WeatherError as error:
        log.error("Weather fetch failed: %s", error)
        ok = False
    print(short_gap_report(store, config, now))
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hourly ingest of BAFU and weather data.")
    parser.add_argument("--store", default="data", help="local folder or gs:// URI")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ok = ingest(
        RawStore(args.store),
        BafuClient(),
        WeatherClient(),
        load_stations(),
        load_pipeline_config(),
        datetime.now(UTC),
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
