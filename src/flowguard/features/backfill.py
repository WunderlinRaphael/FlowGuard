"""Backfill command: BAFU history and weather runs into the raw store.

    uv run python -m flowguard.features.backfill --dry-run
    uv run python -m flowguard.features.backfill

Resumable: every unit (one station and window, or one weather run) is written right after
it was fetched, and units that are already stored are skipped on the next start.
"""

import argparse
import logging
import sys
import time
from datetime import UTC, date, datetime, timedelta

from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.raw_store import RawStore
from flowguard.common.stations import load_stations
from flowguard.features import bafu_client
from flowguard.features.bafu_client import BafuClient, BafuError
from flowguard.features.weather_client import (
    RunNotAvailableError,
    WeatherClient,
    WeatherError,
    select_run,
)

log = logging.getLogger(__name__)

RUN_STEP = timedelta(hours=6)
SOURCES = ("bafu", "weather")


def floor_hour(ts):
    return ts.replace(minute=0, second=0, microsecond=0)


def latest_run(now, config):
    """Start of the newest run the run rule allows at wall-clock time now.

    The pipeline run at `now` sees the hours up to t = now - as_of_offset, so the rule gives
    the same run as select_run does for that origin hour in training.
    """
    t = floor_hour(now) - config.as_of_offset
    return select_run(t, as_of_offset=config.as_of_offset, min_run_age=config.min_run_age)


def run_unit(run_start):
    return f"run-{run_start:%Y%m%dT%H}"


def weather_runs(weather_start, latest):
    """All run starts (every 6 h) from weather_start 00 UTC up to `latest`."""
    run = datetime(weather_start.year, weather_start.month, weather_start.day, tzinfo=UTC)
    runs = []
    while run <= latest:
        runs.append(run)
        run += RUN_STEP
    return runs


def bafu_unit(station, start, end):
    # The end is part of the name: when the last window grows on a later day, it is a new unit.
    return f"backfill-{station.number}-{start:%Y%m%d}-{end:%Y%m%d}"


def bafu_windows(start, end):
    """Consecutive 30-day windows [start, end), the last one shorter."""
    windows = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + bafu_client.MAX_WINDOW, end)
        windows.append((window_start, window_end))
        window_start = window_end
    return windows


def plan_bafu(stations, store, now, start_date):
    """Return (all units, units still to fetch) as lists of (name, station, start, end)."""
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
    # Whole days only, so unit names stay the same during a day.
    end = datetime(now.year, now.month, now.day, tzinfo=UTC)
    units = [
        (bafu_unit(s, ws, we), s, ws, we) for s in stations for ws, we in bafu_windows(start, end)
    ]
    stored = store.units("bafu")
    return units, [u for u in units if u[0] not in stored]


def plan_weather(config, store, now, start_date):
    """Return (all units, units still to fetch) as lists of (name, run_start)."""
    runs = weather_runs(start_date, latest_run(now, config))
    units = [(run_unit(r), r) for r in runs]
    stored = store.units("weather")
    return units, [u for u in units if u[0] not in stored]


def print_dry_run(label, units, todo):
    # One request per unit; the clients wait at least one second between requests.
    minutes = len(todo) * bafu_client.MIN_INTERVAL / 60
    print(
        f"{label}: {len(units)} units, {len(units) - len(todo)} stored, {len(todo)} to do, "
        f"{len(todo)} requests, at least {minutes:.0f} min"
    )


def run_units(label, todo, limit, fetch_unit):
    """Fetch and store up to `limit` units. fetch_unit(unit) returns the number of rows."""
    if limit is not None:
        todo = todo[:limit]
    started = time.monotonic()
    stored = 0
    for i, unit in enumerate(todo, start=1):
        try:
            rows = fetch_unit(unit)
        except RunNotAvailableError as error:
            # A run that does not exist (yet) is not stored, so it is tried again next time.
            log.warning("%s %s: not available (%s)", label, unit[0], error)
            continue
        stored += 1
        elapsed = time.monotonic() - started
        eta = elapsed / i * (len(todo) - i)
        log.info("%s [%d/%d] %s: %d rows, %.0f s left", label, i, len(todo), unit[0], rows, eta)
    return stored


def backfill(
    store,
    bafu,
    weather,
    stations,
    config,
    now,
    sources=SOURCES,
    dry_run=False,
    limit=None,
    bafu_start=None,
    weather_start=None,
):
    """Run the backfill. Returns True if it finished without a fetch error."""
    plans = {}
    if "bafu" in sources:
        plans["bafu"] = plan_bafu(stations, store, now, bafu_start or config.bafu_backfill_start)
    if "weather" in sources:
        plans["weather"] = plan_weather(config, store, now, weather_start or config.weather_start)

    if dry_run:
        for label, (units, todo) in plans.items():
            print_dry_run(label, units, todo)
        return True

    def fetch_bafu(unit):
        _, station, start, end = unit
        rows = bafu.fetch_station(station, start, end)
        store.write_bafu(rows, unit[0])
        return len(rows)

    def fetch_weather(unit):
        name, run_start = unit
        rows = weather.fetch_run(config.weather_points, run_start)
        store.write_weather(rows, name, run_start)
        return len(rows)

    fetchers = {"bafu": fetch_bafu, "weather": fetch_weather}
    for label, (_, todo) in plans.items():
        try:
            run_units(label, todo, limit, fetchers[label])
        except (BafuError, WeatherError) as error:
            # For example HTTP 429 after all retries: stop, the next start continues here.
            log.error("%s stopped: %s. Stored units are kept, run the command again.", label, error)
            return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill BAFU and weather data.")
    parser.add_argument("--store", default="data", help="local folder or gs:// URI")
    parser.add_argument("--dry-run", action="store_true", help="print request counts only")
    parser.add_argument("--limit", type=int, help="maximum requests per source")
    parser.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    parser.add_argument("--bafu-start", type=date.fromisoformat, help="overrides the config")
    parser.add_argument("--weather-start", type=date.fromisoformat, help="overrides the config")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sources = SOURCES if args.source == "all" else (args.source,)
    ok = backfill(
        RawStore(args.store),
        BafuClient(),
        WeatherClient(),
        load_stations(),
        load_pipeline_config(),
        datetime.now(UTC),
        sources=sources,
        dry_run=args.dry_run,
        limit=args.limit,
        bafu_start=args.bafu_start,
        weather_start=args.weather_start,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
