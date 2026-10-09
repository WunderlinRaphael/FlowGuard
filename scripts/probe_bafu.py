"""One-off probe of the BAFU hydrology API.

Prints station metadata and checks which of Q, W and WT arrived in the last
7 days of hourly means, with row counts and missing hours. It also checks
whether an hourly timestamp marks the start or the end of its hour, which
release state old and recent values have, and how old the newest value is.

Run with: uv run python scripts/probe_bafu.py
"""

from collections import Counter
from datetime import UTC, datetime, timedelta

import requests

API_URL = "https://data.bafu.admin.ch/api"
STATIONS = ["2087", "2299", "2491", "2492", "2649", "2056"]
PARAMETERS = ["Q", "W", "WT"]
DAYS = 7
# The API returns at most this many rows per query. A response of exactly this
# size means it was cut off and the window has to be smaller.
ROW_LIMIT = 10_000
# Stations and hours used to compare hourly means with 10-minute means.
HOUR_LABEL_STATIONS = ["2087", "2491", "2056"]
HOUR_LABEL_HOURS = [3, 7, 10, 13, 16, 20]
# How far back (in days) to sample one day of release states.
RELEASE_SAMPLES_DAYS = [30, 60, 90, 180, 365, 730]

STATIONS_QUERY = """
query ($nos: [String!]) {
  water { observations {
    stations(where: { no: { _in: $nos } }) {
      no name riverName status coverageFrom coverageTo
    }
  } }
}
"""

# The series name (data_1hour_mean, data_10min_mean) is part of the query text,
# because GraphQL does not allow a field name as a variable.
SERIES_QUERY = """
query ($station: String!, $from: AWSDateTime!, $to: AWSDateTime!) {
  water { observations {
    SERIES(where: {
      station: { no: { _eq: $station } }
      timestamp: { _gte: $from, _lt: $to }
    }) {
      timestamp parameterName value unitSymbol releaseState
    }
  } }
}
"""


def post_query(query, variables):
    response = requests.post(API_URL, json={"query": query, "variables": variables}, timeout=60)
    response.raise_for_status()
    body = response.json()
    # GraphQL reports problems in "errors" even when the HTTP status is 200.
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]["water"]["observations"]


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    return datetime.fromisoformat(ts)


def fetch_series(series, station, start, end):
    """Fetch a series in 1-day windows so no single query hits the row limit."""
    query = SERIES_QUERY.replace("SERIES", series)
    rows = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=1), end)
        variables = {"station": station, "from": iso(window_start), "to": iso(window_end)}
        chunk = post_query(query, variables)[series]
        if len(chunk) >= ROW_LIMIT:
            print(f"  WARNING: {station} {iso(window_start)} hit the row limit, data is cut off")
        rows.extend(chunk)
        window_start = window_end
    return rows


def summarize(rows, parameter, expected_hours, now):
    values = [r for r in rows if r["parameterName"] == parameter]
    if not values:
        return f"{parameter:<3} missing"
    # Compare timestamps against the full hourly grid, not row positions.
    seen = {parse(r["timestamp"]) for r in values}
    latest = max(seen)
    # Hours after the latest value are publication delay, not holes in the series.
    gaps = len({h for h in expected_hours if h <= latest} - seen)
    lag = int((now - latest).total_seconds() // 3600)
    unit = values[0]["unitSymbol"]
    return (
        f"{parameter:<3} unit={unit:<8} rows={len(values):<4} gaps={gaps:<4} "
        f"latest={iso(latest)} ({lag} h ago)"
    )


def mean(values):
    return sum(values) / len(values) if values else None


def check_hour_label(day):
    """Compare each hourly mean with the 10-minute means just after and just before it."""
    print(f"\nHour label check on {day:%Y-%m-%d} (Q)")
    votes = Counter()
    for no in HOUR_LABEL_STATIONS:
        hourly = fetch_series("data_1hour_mean", no, day, day + timedelta(days=1))
        # One extra hour before the day so the "end" window of 00:00 is complete.
        ten_min = fetch_series(
            "data_10min_mean", no, day - timedelta(hours=1), day + timedelta(days=1)
        )
        hourly = {parse(r["timestamp"]): r["value"] for r in hourly if r["parameterName"] == "Q"}
        ten_min = {parse(r["timestamp"]): r["value"] for r in ten_min if r["parameterName"] == "Q"}
        for hour in HOUR_LABEL_HOURS:
            t = day + timedelta(hours=hour)
            if t not in hourly:
                print(f"  {no} {t:%H:%M} no hourly value")
                continue
            # "start": the value covers [t, t+1h). "end": it covers (t-1h, t].
            start_mean = mean([v for ts, v in ten_min.items() if t <= ts < t + timedelta(hours=1)])
            end_mean = mean([v for ts, v in ten_min.items() if t - timedelta(hours=1) < ts <= t])
            if start_mean is None or end_mean is None:
                print(f"  {no} {t:%H:%M} not enough 10-minute values")
                continue
            diff_start = abs(hourly[t] - start_mean)
            diff_end = abs(hourly[t] - end_mean)
            if diff_start < diff_end:
                vote = "start"
            elif diff_end < diff_start:
                vote = "end"
            else:
                vote = "tie"
            votes[vote] += 1
            print(
                f"  {no} {t:%H:%M} hourly={hourly[t]:.4f} start_mean={start_mean:.4f} "
                f"end_mean={end_mean:.4f} -> {vote}"
            )
    print(f"  Votes: {dict(votes)}")


def check_release_states(now):
    """Count releaseState values in the last 7 days and on sample days further back."""
    print("\nRelease states (1 = provisional, 2 = validated, 3 = definitive, None = not set)")
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for no in STATIONS:
        recent = fetch_series("data_1hour_mean", no, today - timedelta(days=DAYS), today)
        counts = Counter(r["releaseState"] for r in recent)
        print(f"  {no} last {DAYS} days: {dict(counts)}")
        for days_back in RELEASE_SAMPLES_DAYS:
            day = today - timedelta(days=days_back)
            rows = fetch_series("data_1hour_mean", no, day, day + timedelta(days=1))
            counts = Counter(r["releaseState"] for r in rows)
            print(f"    {day:%Y-%m-%d} ({days_back} days back): {dict(counts)}")


def check_newest_age(now):
    """Report how old the newest hourly value is, from its timestamp and from the end of its hour."""
    print(f"\nAge of the newest hourly value (now {iso(now)})")
    for no in STATIONS:
        rows = fetch_series("data_1hour_mean", no, now - timedelta(days=2), now)
        if not rows:
            print(f"  {no} no values in the last 2 days")
            continue
        newest = max(parse(r["timestamp"]) for r in rows)
        age = (now - newest).total_seconds() / 3600
        # If the timestamp marks the start of the hour, the value is complete one hour later.
        age_after_end = age - 1
        print(
            f"  {no} newest={iso(newest)} age={age:.1f} h "
            f"(since its hour ended: {age_after_end:.1f} h)"
        )


def main():
    print("Station metadata")
    stations = post_query(STATIONS_QUERY, {"nos": STATIONS})["stations"]
    found = {s["no"]: s for s in stations}
    for no in STATIONS:
        s = found.get(no)
        if s is None:
            print(f"  {no}: not found")
            continue
        print(
            f"  {s['no']} {s['name']} | river={s['riverName']} | status={s['status']} | "
            f"coverage {s['coverageFrom']} to {s['coverageTo']}"
        )

    now = datetime.now(UTC)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=DAYS)
    expected_hours = {start + timedelta(hours=h) for h in range(DAYS * 24)}
    print(f"\nHourly means {iso(start)} to {iso(end)} ({len(expected_hours)} expected hours)")
    for no in STATIONS:
        rows = fetch_series("data_1hour_mean", no, start, end)
        print(f"  {no} ({len(rows)} rows total)")
        for parameter in PARAMETERS:
            print("    " + summarize(rows, parameter, expected_hours, now))

    yesterday = end.replace(hour=0) - timedelta(days=1)
    check_hour_label(yesterday)
    check_release_states(now)
    check_newest_age(now)


if __name__ == "__main__":
    main()
