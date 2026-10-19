"""Client for the BAFU hydrology GraphQL API.

Fetches raw hourly means (data_1hour_mean) for the stations in
config/stations.yaml and returns them as plain dicts. Nothing is stored here.
"""

import time
from datetime import UTC, datetime, timedelta

import requests

API_URL = "https://data.bafu.admin.ch/api"
# 30 days x 24 hours x 3 parameters = at most 2,160 rows. BAFU rejects queries
# with more than 10,000 rows.
MAX_WINDOW = timedelta(days=30)
# BAFU allows 500 requests per IP in a sliding 5-minute window. One request per
# second stays at 300 at most.
MIN_INTERVAL = 1.0
# BAFU answers 403 when the rate limit is exceeded; the others are temporary server errors.
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5
# Waits 2, 4, 8 and 16 seconds between the attempts.
BACKOFF = 2.0
TIMEOUT = 60

HOURLY_QUERY = """
query ($station: String!, $parameters: [String!], $from: AWSDateTime!, $to: AWSDateTime!) {
  water { observations {
    data_1hour_mean(where: {
      station: { no: { _eq: $station } }
      parameterName: { _in: $parameters }
      timestamp: { _gte: $from, _lt: $to }
    }) {
      timestamp parameterName value unitSymbol releaseState
    }
  } }
}
"""


class BafuError(Exception):
    """The BAFU API returned an error or could not be reached."""


class RowLimitError(BafuError):
    """The query asked for more than 10,000 rows and was rejected."""


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def check_utc(ts):
    # Naive datetimes would be read as local time, which shifts every window.
    if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
        raise ValueError(f"Expected a UTC datetime, got {ts!r}")


class BafuClient:
    def __init__(self, session=None, sleep=time.sleep, clock=time.monotonic):
        # session, sleep and clock can be replaced in tests, so no real
        # requests are sent and no real waiting happens.
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self.last_request = None

    def post_query(self, query, variables):
        """Send one GraphQL query and return the "observations" part of the answer."""
        for attempt in range(MAX_ATTEMPTS):
            self._wait_for_rate_limit()
            try:
                response = self.session.post(
                    API_URL, json={"query": query, "variables": variables}, timeout=TIMEOUT
                )
            except (requests.ConnectionError, requests.Timeout) as error:
                problem = str(error)
            else:
                if response.status_code not in RETRY_STATUS:
                    break
                problem = f"HTTP {response.status_code}"
            if attempt == MAX_ATTEMPTS - 1:
                raise BafuError(f"Giving up after {MAX_ATTEMPTS} attempts: {problem}")
            self.sleep(BACKOFF * 2**attempt)

        response.raise_for_status()
        body = response.json()
        # GraphQL reports problems in "errors" even when the HTTP status is 200.
        # They are not retried, because the same query would fail again.
        if body.get("errors"):
            messages = "; ".join(e.get("message", "") for e in body["errors"])
            if "more than 10000 rows" in messages:
                raise RowLimitError(messages)
            raise BafuError(f"GraphQL errors: {messages}")
        return body["data"]["water"]["observations"]

    def fetch_station(self, station, start, end):
        """Fetch the hourly means of one station for [start, end), in 30-day windows.

        Fetches every parameter listed for the station in config/stations.yaml,
        including WT at Seedorf, which is stored raw but not used as a feature.
        """
        check_utc(start)
        check_utc(end)
        parameters = list(station.parameters)
        if not parameters:
            return []

        rows = []
        window_start = start
        while window_start < end:
            window_end = min(window_start + MAX_WINDOW, end)
            variables = {
                "station": station.number,
                "parameters": parameters,
                "from": iso(window_start),
                "to": iso(window_end),
            }
            result = self.post_query(HOURLY_QUERY, variables)["data_1hour_mean"]
            fetched_at = datetime.now(UTC)
            rows.extend(to_row(station.number, r, fetched_at) for r in result)
            window_start = window_end
        return rows

    def fetch_stations(self, stations, start, end):
        """Fetch all stations one after the other (no parallel requests)."""
        rows = []
        for station in stations:
            rows.extend(self.fetch_station(station, start, end))
        return rows

    def _wait_for_rate_limit(self):
        if self.last_request is not None:
            wait = MIN_INTERVAL - (self.clock() - self.last_request)
            if wait > 0:
                self.sleep(wait)
        self.last_request = self.clock()


def to_row(station_number, record, fetched_at):
    """Turn one API record into a raw row. Values are kept exactly as delivered."""
    return {
        "station": station_number,
        "timestamp": datetime.fromisoformat(record["timestamp"]),
        "parameter": record["parameterName"],
        "value": record["value"],
        "unit": record["unitSymbol"],
        "release_state": record["releaseState"],
        "fetched_at": fetched_at,
    }
