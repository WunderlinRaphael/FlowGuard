"""Client for the Open-Meteo weather APIs.

Fetches raw hourly weather for the points in config/pipeline.yaml and returns it as plain
dicts. Nothing is stored here. Two sources, as in the live system:
- observed hours from the Historical Forecast API,
- one forecast run (ECMWF IFS HRES, 48 h) from the Single Runs API.
"""

import time
from datetime import UTC, datetime, timedelta

import requests

from flowguard.features.bafu_client import check_utc

SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
MODEL = "ecmwf_ifs"
VARIABLES = ("precipitation", "temperature_2m", "snowfall")
# 48 h per run is enough to cover t+1 to t+24 for the oldest run the as-of rule can pick.
RUN_HOURS_AHEAD = 48
# 30 days x 24 hours x 3 variables x 3 points stays a small answer. Same window as BAFU.
MAX_WINDOW = timedelta(days=30)
# Open-Meteo allows 600 requests per minute; one per second stays far below that.
MIN_INTERVAL = 1.0
# Open-Meteo answers 429 when a limit is exceeded; the others are temporary server errors.
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5
# Waits 2, 4, 8 and 16 seconds between the attempts.
BACKOFF = 2.0
TIMEOUT = 60


class WeatherError(Exception):
    """Open-Meteo returned an error or could not be reached."""


class RunNotAvailableError(WeatherError):
    """The requested model run does not exist (yet)."""


def select_run(t, *, as_of_offset, min_run_age):
    """Return the start of the forecast run to use for the origin hour t.

    The pipeline knows what was available at the as-of time t + as_of_offset. It uses the
    newest run that started at least min_run_age before that time. The answer depends only
    on t and the two config values, never on the wall clock, so a backfill picks the same
    run the live pipeline would have picked.
    """
    check_utc(t)
    if t != t.replace(minute=0, second=0, microsecond=0):
        raise ValueError(f"Expected a timestamp on the hour, got {t!r}")
    latest_start = t + as_of_offset - min_run_age
    return latest_start.replace(hour=latest_start.hour // 6 * 6, minute=0, second=0, microsecond=0)


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M")


class WeatherClient:
    def __init__(self, session=None, sleep=time.sleep, clock=time.monotonic):
        # session, sleep and clock can be replaced in tests, so no real
        # requests are sent and no real waiting happens.
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self.last_request = None

    def get(self, url, params):
        """Send one GET request and return the parsed answer."""
        for attempt in range(MAX_ATTEMPTS):
            self._wait_for_rate_limit()
            try:
                response = self.session.get(url, params=params, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as error:
                problem = str(error)
            else:
                if response.status_code not in RETRY_STATUS:
                    break
                problem = f"HTTP {response.status_code}"
            if attempt == MAX_ATTEMPTS - 1:
                raise WeatherError(f"Giving up after {MAX_ATTEMPTS} attempts: {problem}")
            self.sleep(BACKOFF * 2**attempt)

        try:
            body = response.json()
        except ValueError:
            raise WeatherError(f"HTTP {response.status_code}, answer is not JSON") from None
        # Open-Meteo reports problems as {"error": true, "reason": ...}, also with HTTP 400.
        # They are not retried, because the same request would fail again.
        if isinstance(body, dict) and body.get("error"):
            reason = body.get("reason", f"HTTP {response.status_code}")
            if "run is not available" in reason:
                raise RunNotAvailableError(reason)
            raise WeatherError(reason)
        response.raise_for_status()
        return body

    def fetch_run(self, points, run_start):
        """Fetch one forecast run (48 h) for all points in a single request.

        Hour 0 of a run is dropped: the precipitation of that hour is always empty.
        """
        check_utc(run_start)
        params = {
            **self._location_params(points),
            "hourly": ",".join(VARIABLES),
            "models": MODEL,
            "run": iso(run_start),
            "forecast_hours": RUN_HOURS_AHEAD,
            "timezone": "GMT",
        }
        body = self.get(SINGLE_RUNS_URL, params)
        fetched_at = datetime.now(UTC)
        rows = to_rows(points, body, run_start, fetched_at)
        return [r for r in rows if r["valid_time"] != run_start]

    def fetch_observed(self, points, start, end):
        """Fetch the observed hours in [start, end), in 30-day windows.

        These are the first hours of the model runs (Historical Forecast API), which
        equal the past hours the live API shows.
        """
        check_utc(start)
        check_utc(end)
        rows = []
        window_start = start
        while window_start < end:
            window_end = min(window_start + MAX_WINDOW, end)
            params = {
                **self._location_params(points),
                "hourly": ",".join(VARIABLES),
                "models": MODEL,
                "start_date": window_start.date().isoformat(),
                # end_date is inclusive, window_end is not.
                "end_date": (window_end - timedelta(hours=1)).date().isoformat(),
                "timezone": "GMT",
            }
            body = self.get(HISTORICAL_URL, params)
            fetched_at = datetime.now(UTC)
            # The API returns whole days, so cut the window to the hours that were asked for.
            rows.extend(
                r
                for r in to_rows(points, body, None, fetched_at)
                if window_start <= r["valid_time"] < window_end
            )
            window_start = window_end
        return rows

    def _location_params(self, points):
        return {
            "latitude": ",".join(str(p.latitude) for p in points),
            "longitude": ",".join(str(p.longitude) for p in points),
        }

    def _wait_for_rate_limit(self):
        if self.last_request is not None:
            wait = MIN_INTERVAL - (self.clock() - self.last_request)
            if wait > 0:
                self.sleep(wait)
        self.last_request = self.clock()


def to_rows(points, body, run_start, fetched_at):
    """Turn an answer into raw rows. Values are kept exactly as delivered (null stays None)."""
    # One location comes back as a dict, several as a list in the order of the request.
    locations = [body] if isinstance(body, dict) else body
    if len(locations) != len(points):
        raise WeatherError(f"Asked for {len(points)} locations, got {len(locations)}")

    rows = []
    for point, location in zip(points, locations, strict=True):
        hourly = location["hourly"]
        units = location["hourly_units"]
        for variable in VARIABLES:
            for valid_time, value in zip(hourly["time"], hourly[variable], strict=True):
                rows.append(
                    {
                        "point": point.name,
                        "valid_time": datetime.fromisoformat(valid_time).replace(tzinfo=UTC),
                        "run_start": run_start,
                        "parameter": variable,
                        "value": value,
                        "unit": units[variable],
                        "fetched_at": fetched_at,
                    }
                )
    return rows
