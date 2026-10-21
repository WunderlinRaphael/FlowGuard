"""One-off probe of the Open-Meteo claims in the weather plan.

Takes the coordinates of the three target stations from BAFU, then checks:
1. Single Runs API (ecmwf_ifs): what a 48 h run returns, units, runs before 14.03.2024.
2. Availability: the newest fetchable run per cycle today. With --poll it waits for the
   next runs and measures the delay between a run's start and its first successful fetch.
3. Historical Forecast API against the Single Runs values for the same hours.
4. Live forecast API (past_days) against the Historical Forecast API.
5. Request counts, response times, rate-limit headers and a backfill estimate.

Run with: uv run python scripts/probe_weather.py
Measure the delay:  uv run python scripts/probe_weather.py --poll --cycles 2
"""

import argparse
import time
from datetime import UTC, date, datetime, timedelta

import requests

BAFU_URL = "https://data.bafu.admin.ch/api"
SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
LIVE_URL = "https://api.open-meteo.com/v1/forecast"

STATIONS = ["2087", "2491", "2056"]
MODEL = "ecmwf_ifs"
VARIABLES = ["precipitation", "temperature_2m", "snowfall"]
RUN_HOURS = [0, 6, 12, 18]
# One mid-month day each: April 2024, January 2025, September 2026.
SAMPLE_DAYS = [date(2024, 4, 16), date(2025, 1, 14), date(2026, 9, 15)]
# The plan says runs exist from 14.03.2024.
FIRST_RUN_DAY = date(2024, 3, 14)
HORIZON = 48
PAUSE = 0.3  # seconds between requests, to stay far below 600 per minute
POLL_INTERVAL = 300  # seconds between tries in --poll mode
DAILY_LIMIT = 10_000  # calls per day on the free tier, per the Open-Meteo pricing page

BAFU_QUERY = """
query ($nos: [String!]) {
  water { observations {
    stations(where: { no: { _in: $nos } }) { no name latitude longitude }
  } }
}
"""

# Collected by get() and reported in section 5.
stats = {"requests": 0, "seconds": [], "status": {}, "headers": {}}


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M")


def get_stations():
    response = requests.post(
        BAFU_URL, json={"query": BAFU_QUERY, "variables": {"nos": STATIONS}}, timeout=60
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    found = {s["no"]: s for s in body["data"]["water"]["observations"]["stations"]}
    return [found[no] for no in STATIONS]


def get(url, **params):
    """GET one Open-Meteo URL. Returns (body, problem); problem is None when it worked."""
    time.sleep(PAUSE)
    started = time.monotonic()
    response = requests.get(url, params=params, timeout=60)
    stats["requests"] += 1
    stats["seconds"].append(time.monotonic() - started)
    stats["status"][response.status_code] = stats["status"].get(response.status_code, 0) + 1
    for name, value in response.headers.items():
        if name.lower().startswith("x-ratelimit") or name.lower() == "retry-after":
            stats["headers"][name] = value
    try:
        body = response.json()
    except ValueError:
        return None, f"HTTP {response.status_code}, not JSON"
    # Open-Meteo reports problems as {"error": true, "reason": ...}, also with HTTP 400.
    if isinstance(body, dict) and body.get("error"):
        return None, body.get("reason", f"HTTP {response.status_code}")
    return body, None


def single_run(station, run, hours=HORIZON):
    return get(
        SINGLE_RUNS_URL,
        latitude=station["latitude"],
        longitude=station["longitude"],
        hourly=",".join(VARIABLES),
        models=MODEL,
        run=iso(run),
        forecast_hours=hours,
    )


def historical(station, first_day, last_day):
    return get(
        HISTORICAL_URL,
        latitude=station["latitude"],
        longitude=station["longitude"],
        hourly=",".join(VARIABLES),
        models=MODEL,
        start_date=first_day.isoformat(),
        end_date=last_day.isoformat(),
    )


def series(body):
    """Turn the hourly block into {variable: {time string: value}}."""
    hourly = body["hourly"]
    return {v: dict(zip(hourly["time"], hourly[v], strict=True)) for v in VARIABLES}


def nums(values):
    return [v for v in values if v is not None]


def check_single_runs(stations):
    """Section 1: one 48 h run per sample day, cycle and station."""
    print("\n1. Single Runs API (ecmwf_ifs), 48 h per run")
    runs = {}  # (station no, run) -> series, reused in section 3
    for day in SAMPLE_DAYS:
        for hour in RUN_HOURS:
            run = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
            for station in stations:
                body, problem = single_run(station, run)
                if problem:
                    print(f"  {iso(run)} {station['no']}: {problem}")
                    continue
                data = series(body)
                runs[(station["no"], run)] = data
                first = data["precipitation"][body["hourly"]["time"][0]]
                precip = nums(data["precipitation"].values())
                temp = nums(data["temperature_2m"].values())
                snow = nums(data["snowfall"].values())
                units = body["hourly_units"]
                print(
                    f"  {iso(run)} {station['no']}: rows={len(body['hourly']['time'])} "
                    f"first_precip={first} precip_sum={sum(precip):.1f}{units['precipitation']} "
                    f"temp={min(temp):.1f}..{max(temp):.1f}{units['temperature_2m']} "
                    f"snow_sum={sum(snow):.1f}{units['snowfall']} elev={body['elevation']:.0f}m"
                )

    print("\n  Runs around the start of the archive (Andermatt point)")
    for day in [FIRST_RUN_DAY - timedelta(days=60), FIRST_RUN_DAY - timedelta(days=1)]:
        run = datetime(day.year, day.month, day.day, 0, tzinfo=UTC)
        _, problem = single_run(stations[0], run, hours=1)
        print(f"  {iso(run)}: {problem or 'available'}")
    for hour in RUN_HOURS:
        run = datetime(FIRST_RUN_DAY.year, 3, 14, hour, tzinfo=UTC)
        _, problem = single_run(stations[0], run, hours=1)
        print(f"  {iso(run)}: {problem or 'available'}")
    return runs


def run_exists(station, day, hour):
    run = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
    _, problem = single_run(station, run, hours=1)
    return problem is None


def check_cycle_history(station):
    """Section 1c: since when do the 06Z and 18Z runs exist? (April 2024 had only 00Z and 12Z.)"""
    print("\n1c. When do the 06Z and 18Z runs start? (Andermatt point)")
    firsts = [date(2024, m, 1) for m in range(4, 13)] + [date(2025, 1, 1)]
    present = None
    for day in firsts:
        found = {h: run_exists(station, day, h) for h in (6, 18)}
        print(f"  {day}: 06Z {found[6]}, 18Z {found[18]}")
        if all(found.values()):
            present = day
            break
    if present is None:
        return
    # Daily scan in the month before the first month where both exist.
    day = present - timedelta(days=31)
    while day <= present:
        if run_exists(station, day, 6) and run_exists(station, day, 18):
            print(f"  first day with all four runs: {day}")
            return
        day += timedelta(days=1)


def check_availability(station, now):
    """Section 2: which of the runs of the last two days can be fetched right now."""
    print(f"\n2. Availability now ({iso(now)} UTC), Andermatt point")
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = [
        today + timedelta(days=d, hours=h)
        for d in (-1, 0)
        for h in RUN_HOURS
        if today + timedelta(days=d, hours=h) <= now
    ]
    newest = None
    for run in sorted(candidates, reverse=True):
        _, problem = single_run(station, run, hours=1)
        age = (now - run).total_seconds() / 3600
        print(f"  run {iso(run)} (started {age:.1f} h ago): {problem or 'available'}")
        if problem is None and newest is None:
            newest = run
    if newest is not None:
        age = (now - newest).total_seconds() / 3600
        print(
            f"  Newest available run started {age:.1f} h ago. The delay of that run is at "
            f"most {age:.1f} h; the next run (started {age - 6:.1f} h ago) is not out yet."
        )


def poll_delays(station, cycles):
    """Section 2b: wait for the next runs and time the first successful fetch of each."""
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Start with the run that began in the last 6 h, in case it is not out yet.
    upcoming = [
        today + timedelta(days=d, hours=h)
        for d in (0, 1)
        for h in RUN_HOURS
        if now - timedelta(hours=6) < today + timedelta(days=d, hours=h)
    ][:cycles]
    print(f"\n2b. Polling every {POLL_INTERVAL} s for runs: {[iso(r) for r in upcoming]}")
    for run in upcoming:
        wait = (run - datetime.now(UTC)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        first_try = True
        while True:
            _, problem = single_run(station, run, hours=1)
            now = datetime.now(UTC)
            if problem is None:
                delay = (now - run).total_seconds() / 60
                sign = "at most " if first_try else ""
                print(
                    f"  run {iso(run)}: first success at {iso(now)}, delay {sign}{delay:.0f} min",
                    flush=True,
                )
                break
            first_try = False
            time.sleep(POLL_INTERVAL)
        # The run was first seen at most POLL_INTERVAL seconds before this time.
        print(f"    (precision: +/- {POLL_INTERVAL // 60} min)", flush=True)


def compare(a, b):
    """Return (mean absolute difference, max absolute difference, n) for two aligned lists."""
    diffs = [abs(x - y) for x, y in zip(a, b, strict=True) if x is not None and y is not None]
    if not diffs:
        return None
    return sum(diffs) / len(diffs), max(diffs), len(diffs)


def fmt(result):
    if result is None:
        return "no overlap"
    mean_diff, max_diff, n = result
    return f"mean {mean_diff:.2f} max {max_diff:.2f} (n={n})"


def check_historical(stations, runs):
    """Section 3: Single Runs values against the Historical Forecast API, same valid hours."""
    print("\n3. Historical Forecast API against Single Runs (absolute differences)")
    for day in SAMPLE_DAYS:
        for station in stations:
            body, problem = historical(station, day, day + timedelta(days=2))
            if problem:
                print(f"  {day} {station['no']}: {problem}")
                continue
            hist = series(body)
            print(f"  {day} {station['no']} (hourly rows: {len(body['hourly']['time'])})")
            for hour in RUN_HOURS:
                run = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
                single = runs.get((station["no"], run))
                if single is None:
                    continue
                for label, lead in [
                    ("hours +1..+5", range(1, 6)),
                    ("hours +24..+47", range(24, 48)),
                ]:
                    parts = []
                    for variable in VARIABLES:
                        times = [iso(run + timedelta(hours=h)) for h in lead]
                        a = [single[variable].get(t) for t in times]
                        b = [hist[variable].get(t) for t in times]
                        parts.append(f"{variable}: {fmt(compare(a, b))}")
                    print(f"    run {hour:02d}Z {label}: " + " | ".join(parts))


def check_live(station, now):
    """Section 4: past hours of the live API against the Historical Forecast API."""
    print("\n4. Live API (past_days=3) against Historical Forecast API, Andermatt point")
    body, problem = get(
        LIVE_URL,
        latitude=station["latitude"],
        longitude=station["longitude"],
        hourly=",".join(VARIABLES),
        models=MODEL,
        past_days=3,
        forecast_days=1,
    )
    if problem:
        print(f"  live: {problem}")
        return
    live = series(body)
    today = now.date()
    first_day = today - timedelta(days=3)
    hist_body, problem = historical(station, first_day, today)
    if problem:
        print(f"  historical: {problem}")
        return
    hist = series(hist_body)
    last_hist = max(t for t, v in hist["temperature_2m"].items() if v is not None)
    print(
        f"  live rows {len(body['hourly']['time'])}, historical rows {len(hist_body['hourly']['time'])}"
    )
    print(f"  newest historical hour with a value: {last_hist}")
    for offset in (3, 2, 1):
        day = today - timedelta(days=offset)
        times = [
            iso(datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(hours=h))
            for h in range(24)
        ]
        parts = []
        for variable in VARIABLES:
            a = [live[variable].get(t) for t in times]
            b = [hist[variable].get(t) for t in times]
            parts.append(f"{variable}: {fmt(compare(a, b))}")
        print(f"  {day}: " + " | ".join(parts))


def check_multi_location(stations):
    """Section 5b: can one request carry all three locations?"""
    print("\n5b. Several locations in one Single Runs request")
    run = datetime(2026, 9, 15, 0, tzinfo=UTC)
    response = requests.get(
        SINGLE_RUNS_URL,
        params={
            "latitude": ",".join(str(s["latitude"]) for s in stations),
            "longitude": ",".join(str(s["longitude"]) for s in stations),
            "hourly": "precipitation",
            "models": MODEL,
            "run": iso(run),
            "forecast_hours": 24,
        },
        timeout=60,
    )
    stats["requests"] += 1
    body = response.json()
    if isinstance(body, list):
        print(
            f"  HTTP {response.status_code}: list of {len(body)} locations, "
            f"{len(body[0]['hourly']['time'])} rows each"
        )
    else:
        print(f"  HTTP {response.status_code}: {body}")


def report_stats(now):
    print("\n5. Requests, timing, limits")
    seconds = stats["seconds"]
    print(f"  requests sent (with get()): {len(seconds)}, status codes: {stats['status']}")
    print(f"  response time: mean {sum(seconds) / len(seconds):.2f} s, max {max(seconds):.2f} s")
    print(f"  rate-limit headers seen: {stats['headers'] or 'none'}")
    days = (now.date() - FIRST_RUN_DAY).days + 1
    per_location = days * len(RUN_HOURS)
    print(
        f"  backfill from {FIRST_RUN_DAY}: {days} days x {len(RUN_HOURS)} runs = {per_location} runs"
    )
    print(
        f"    one location per request, 3 locations: {per_location * 3} requests "
        f"(~{per_location * 3 / DAILY_LIMIT:.1f} days at {DAILY_LIMIT} per day)"
    )
    print(f"    3 locations in one request: {per_location} requests")
    print(f"    live: 4 runs per day x 3 locations = {4 * 3} requests per day, or 4 with one call")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll", action="store_true", help="measure the delay of the next runs")
    parser.add_argument("--cycles", type=int, default=2, help="runs to wait for with --poll")
    args = parser.parse_args()

    stations = get_stations()
    print("Locations (BAFU)")
    for s in stations:
        print(f"  {s['no']} {s['name']}: lat {s['latitude']:.4f}, lon {s['longitude']:.4f}")

    if args.poll:
        poll_delays(stations[0], args.cycles)
        return

    now = datetime.now(UTC)
    runs = check_single_runs(stations)
    check_cycle_history(stations[0])
    check_availability(stations[0], now)
    check_historical(stations, runs)
    check_live(stations[0], now)
    check_multi_location(stations)
    report_stats(now)


if __name__ == "__main__":
    main()
