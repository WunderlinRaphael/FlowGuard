"""One-off probe of the BAFU hydrology API.

Prints station metadata and checks which of Q, W and WT arrived in the last
7 days of hourly means, with row counts and missing hours.

Run with: uv run python scripts/probe_bafu.py
"""

from datetime import UTC, datetime, timedelta

import requests

API_URL = "https://data.bafu.admin.ch/api"
STATIONS = ["2087", "2299", "2491", "2492", "2649", "2056"]
PARAMETERS = ["Q", "W", "WT"]
DAYS = 7
# The API returns at most this many rows per query. A response of exactly this
# size means it was cut off and the window has to be smaller.
ROW_LIMIT = 10_000

STATIONS_QUERY = """
query ($nos: [String!]) {
  water { observations {
    stations(where: { no: { _in: $nos } }) {
      no name riverName status coverageFrom coverageTo
    }
  } }
}
"""

HOURLY_QUERY = """
query ($station: String!, $from: AWSDateTime!, $to: AWSDateTime!) {
  water { observations {
    data_1hour_mean(where: {
      station: { no: { _eq: $station } }
      timestamp: { _gte: $from, _lt: $to }
    }) {
      timestamp parameterName value unitSymbol
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


def fetch_hourly(station, start, end):
    """Fetch hourly means in 1-day windows so no single query hits the row limit."""
    rows = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=1), end)
        variables = {"station": station, "from": iso(window_start), "to": iso(window_end)}
        chunk = post_query(HOURLY_QUERY, variables)["data_1hour_mean"]
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
    seen = {datetime.fromisoformat(r["timestamp"]) for r in values}
    latest = max(seen)
    # Hours after the latest value are publication delay, not holes in the series.
    gaps = len({h for h in expected_hours if h <= latest} - seen)
    lag = int((now - latest).total_seconds() // 3600)
    unit = values[0]["unitSymbol"]
    return (
        f"{parameter:<3} unit={unit:<8} rows={len(values):<4} gaps={gaps:<4} "
        f"latest={iso(latest)} ({lag} h ago)"
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
        rows = fetch_hourly(no, start, end)
        print(f"  {no} ({len(rows)} rows total)")
        for parameter in PARAMETERS:
            print("    " + summarize(rows, parameter, expected_hours, now))


if __name__ == "__main__":
    main()
