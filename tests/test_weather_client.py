"""Tests for the weather client. A fake session replaces the network, so no real calls are made."""

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
import requests

from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.features.weather_client import (
    BACKOFF,
    MAX_ATTEMPTS,
    MAX_WINDOW,
    MIN_INTERVAL,
    RunNotAvailableError,
    WeatherClient,
    WeatherError,
    select_run,
)

FIXTURES = Path(__file__).parent / "fixtures"
SINGLE_RUN = json.loads((FIXTURES / "openmeteo_single_run.json").read_text())
HISTORICAL = json.loads((FIXTURES / "openmeteo_historical.json").read_text())

POINTS = load_pipeline_config().weather_points
RUN = datetime(2026, 10, 20, 0, tzinfo=UTC)
AS_OF_OFFSET = timedelta(hours=3)
MIN_RUN_AGE = timedelta(hours=7)
# Slowest delay measured on 20./21.10.2026 (355 min).
MEASURED_MAX_DELAY = timedelta(minutes=355)


# A datetime without time zone, which the client must reject.
NAIVE = datetime(2026, 10, 21, 10)  # noqa: DTZ001


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def choose(t):
    return select_run(t, as_of_offset=AS_OF_OFFSET, min_run_age=MIN_RUN_AGE)


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Returns the queued responses in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params, timeout):
        self.requests.append((url, params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClock:
    """A clock that only moves when the client sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(*responses):
    clock = FakeClock()
    session = FakeSession(*responses)
    return WeatherClient(session=session, sleep=clock.sleep, clock=clock.time), session, clock


def location(times, precipitation=None):
    """A minimal answer for one location, in the shape Open-Meteo sends."""
    n = len(times)
    return {
        "hourly_units": {
            "time": "iso8601",
            "precipitation": "mm",
            "temperature_2m": "°C",
            "snowfall": "cm",
        },
        "hourly": {
            "time": times,
            "precipitation": precipitation or [0.5] * n,
            "temperature_2m": [5.0] * n,
            "snowfall": [0.0] * n,
        },
    }


# --- select_run ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("t", "expected"),
    [
        # One origin hour for each of the four cycles.
        (utc(2026, 10, 21, 10), utc(2026, 10, 21, 6)),
        (utc(2026, 10, 21, 16), utc(2026, 10, 21, 12)),
        (utc(2026, 10, 21, 22), utc(2026, 10, 21, 18)),
        (utc(2026, 10, 21, 4), utc(2026, 10, 21, 0)),
        # At t = 10:00 the 06Z run is exactly 7 h old at the as-of time (13:00): old enough.
        # One hour earlier it is not, so the previous cycle is used.
        (utc(2026, 10, 21, 9), utc(2026, 10, 21, 0)),
        # Day and year rollover.
        (utc(2026, 10, 21, 3), utc(2026, 10, 20, 18)),
        (utc(2027, 1, 1, 0), utc(2026, 12, 31, 18)),
    ],
)
def test_select_run(t, expected):
    assert choose(t) == expected


def test_select_run_does_not_depend_on_the_clock():
    # There is no clock argument: the same t always gives the same run.
    t = utc(2026, 10, 21, 13)
    assert choose(t) == choose(t) == utc(2026, 10, 21, 6)


@pytest.mark.parametrize("t", [NAIVE, utc(2026, 10, 21, 10, 30)])
def test_select_run_rejects_bad_timestamps(t):
    with pytest.raises(ValueError):
        choose(t)


def test_chosen_run_was_available_at_the_as_of_time():
    # Every origin hour of a week (it crosses a month end): the chosen run
    # must be out at t + 3 h, must be the newest such cycle, and must start on a cycle hour.
    t = utc(2026, 10, 28)
    while t < utc(2026, 11, 5):
        run = choose(t)
        as_of = t + AS_OF_OFFSET
        assert run.hour in (0, 6, 12, 18)
        assert run + MIN_RUN_AGE <= as_of
        assert run + MEASURED_MAX_DELAY <= as_of
        # The next cycle would be too young: no newer usable run was skipped.
        assert run + timedelta(hours=6) + MIN_RUN_AGE > as_of
        # A 48 h run still covers t + 24 h.
        assert t + timedelta(hours=24) <= run + timedelta(hours=47)
        t += timedelta(hours=1)


# --- fetch_run ----------------------------------------------------------------------------


def test_fetch_run_request():
    client, session, _ = make_client(FakeResponse(200, SINGLE_RUN))
    client.fetch_run(POINTS, RUN)

    assert len(session.requests) == 1
    url, params = session.requests[0]
    assert url == "https://single-runs-api.open-meteo.com/v1/forecast"
    assert params["models"] == "ecmwf_ifs"
    assert params["hourly"] == "precipitation,temperature_2m,snowfall"
    assert params["run"] == "2026-10-20T00:00"
    assert params["forecast_hours"] == 48
    assert params["timezone"] == "GMT"
    assert params["latitude"] == "46.6423,46.73,46.88"
    assert params["longitude"] == "8.5897,8.47,8.88"


def test_fetch_run_returns_raw_rows_without_hour_0():
    client, _, _ = make_client(FakeResponse(200, SINGLE_RUN))
    rows = client.fetch_run(POINTS, RUN)

    # 3 points x 3 variables x 47 hours (hour 0 dropped).
    assert len(rows) == 3 * 3 * 47
    assert {r["point"] for r in rows} == {"andermatt", "meiental", "schaechental"}
    assert {r["parameter"] for r in rows} == {"precipitation", "temperature_2m", "snowfall"}
    assert min(r["valid_time"] for r in rows) == RUN + timedelta(hours=1)
    assert max(r["valid_time"] for r in rows) == RUN + timedelta(hours=47)
    assert all(r["run_start"] == RUN for r in rows)
    assert all(r["valid_time"].tzinfo == UTC for r in rows)
    assert all(r["fetched_at"].tzinfo == UTC for r in rows)
    units = {r["parameter"]: r["unit"] for r in rows}
    assert units == {"precipitation": "mm", "temperature_2m": "°C", "snowfall": "cm"}


def test_recorded_run_has_null_precipitation_at_hour_0():
    # The recording shows why hour 0 is dropped.
    for loc in SINGLE_RUN:
        assert loc["hourly"]["time"][0] == "2026-10-20T00:00"
        assert loc["hourly"]["precipitation"][0] is None


def test_fetch_run_never_returns_hour_0_for_any_variable():
    client, _, _ = make_client(FakeResponse(200, SINGLE_RUN))
    rows = client.fetch_run(POINTS, RUN)
    assert not [r for r in rows if r["valid_time"] == RUN]
    assert not [r for r in rows if r["parameter"] == "precipitation" and r["value"] is None]


def test_fetch_run_keeps_later_nulls_as_missing():
    times = ["2026-10-20T00:00", "2026-10-20T01:00", "2026-10-20T02:00"]
    answer = location(times, precipitation=[None, None, 1.0])
    client, _, _ = make_client(FakeResponse(200, answer))
    rows = client.fetch_run(POINTS[:1], RUN)

    precipitation = [r["value"] for r in rows if r["parameter"] == "precipitation"]
    assert precipitation == [None, 1.0]


def test_single_location_answer_is_a_dict():
    times = ["2026-10-20T00:00", "2026-10-20T01:00"]
    client, _, _ = make_client(FakeResponse(200, location(times)))
    rows = client.fetch_run(POINTS[:1], RUN)
    assert len(rows) == 3
    assert {r["point"] for r in rows} == {"andermatt"}


def test_empty_hourly_gives_no_rows():
    client, _, _ = make_client(FakeResponse(200, [location([])] * 3))
    assert client.fetch_run(POINTS, RUN) == []


def test_empty_list_is_an_error():
    client, _, _ = make_client(FakeResponse(200, []))
    with pytest.raises(WeatherError, match="Asked for 3 locations, got 0"):
        client.fetch_run(POINTS, RUN)


def test_wrong_number_of_locations_is_an_error():
    client, _, _ = make_client(FakeResponse(200, SINGLE_RUN[:2]))
    with pytest.raises(WeatherError, match="got 2"):
        client.fetch_run(POINTS, RUN)


def test_naive_run_start_is_rejected():
    client, session, _ = make_client()
    with pytest.raises(ValueError):
        client.fetch_run(POINTS, NAIVE)
    assert session.requests == []


# --- fetch_observed -----------------------------------------------------------------------


def test_fetch_observed_request_and_rows():
    client, session, _ = make_client(FakeResponse(200, HISTORICAL))
    rows = client.fetch_observed(POINTS, utc(2026, 10, 18), utc(2026, 10, 20))

    url, params = session.requests[0]
    assert url == "https://historical-forecast-api.open-meteo.com/v1/forecast"
    assert params["models"] == "ecmwf_ifs"
    assert params["start_date"] == "2026-10-18"
    assert params["end_date"] == "2026-10-19"
    # 3 points x 3 variables x 48 hours, and hour 0 is a real observed hour here.
    assert len(rows) == 3 * 3 * 48
    assert all(r["run_start"] is None for r in rows)
    assert min(r["valid_time"] for r in rows) == utc(2026, 10, 18)


def test_fetch_observed_cuts_the_answer_to_the_asked_hours():
    client, session, _ = make_client(FakeResponse(200, HISTORICAL))
    rows = client.fetch_observed(POINTS, utc(2026, 10, 18, 6), utc(2026, 10, 19, 12))

    assert session.requests[0][1]["end_date"] == "2026-10-19"
    assert min(r["valid_time"] for r in rows) == utc(2026, 10, 18, 6)
    assert max(r["valid_time"] for r in rows) == utc(2026, 10, 19, 11)
    assert len(rows) == 3 * 3 * 30


def test_fetch_observed_end_at_midnight_does_not_ask_for_the_next_day():
    client, session, _ = make_client(FakeResponse(200, HISTORICAL))
    client.fetch_observed(POINTS, utc(2026, 10, 18), utc(2026, 10, 19))
    assert session.requests[0][1]["end_date"] == "2026-10-18"


def test_fetch_observed_uses_windows_of_at_most_30_days():
    empty = FakeResponse(200, [location([])] * 3)
    client, session, _ = make_client(empty, empty)
    start = utc(2026, 8, 1)
    client.fetch_observed(POINTS, start, start + timedelta(days=45))

    assert len(session.requests) == 2
    first, second = (params for _, params in session.requests)
    assert first["start_date"] == "2026-08-01"
    assert second["start_date"] == (start + MAX_WINDOW).date().isoformat()
    assert second["end_date"] == "2026-09-14"


# --- errors, retries, pacing --------------------------------------------------------------


def test_retries_429_then_succeeds():
    client, session, clock = make_client(FakeResponse(429), FakeResponse(200, SINGLE_RUN))
    rows = client.fetch_run(POINTS, RUN)

    assert len(rows) == 3 * 3 * 47
    assert len(session.requests) == 2
    assert clock.sleeps == [BACKOFF]


def test_retries_connection_error():
    client, session, _ = make_client(
        requests.ConnectionError("down"), FakeResponse(200, SINGLE_RUN)
    )
    client.fetch_run(POINTS, RUN)
    assert len(session.requests) == 2


def test_gives_up_after_max_attempts_with_growing_waits():
    client, session, clock = make_client(*[FakeResponse(503)] * MAX_ATTEMPTS)
    with pytest.raises(WeatherError, match="Giving up after 5 attempts: HTTP 503"):
        client.fetch_run(POINTS, RUN)

    assert len(session.requests) == MAX_ATTEMPTS
    assert clock.sleeps == [2.0, 4.0, 8.0, 16.0]


def test_error_body_is_not_retried():
    body = {"error": True, "reason": "Latitude must be in range of -90 to 90"}
    client, session, _ = make_client(FakeResponse(400, body))
    with pytest.raises(WeatherError, match="Latitude"):
        client.fetch_run(POINTS, RUN)
    assert len(session.requests) == 1


def test_run_that_does_not_exist():
    body = {
        "error": True,
        "reason": "The requested model run is not available. Model: ecmwf_ifs, run: 2024-01-01T00:00Z",
    }
    client, session, _ = make_client(FakeResponse(400, body))
    with pytest.raises(RunNotAvailableError):
        client.fetch_run(POINTS, utc(2024, 1, 1))
    assert len(session.requests) == 1


def test_answer_that_is_not_json():
    class NotJson(FakeResponse):
        def json(self):
            raise ValueError("no JSON")

    client, _, _ = make_client(NotJson(200))
    with pytest.raises(WeatherError, match="not JSON"):
        client.fetch_run(POINTS, RUN)


def test_waits_between_requests():
    empty = FakeResponse(200, [location([])] * 3)
    client, _, clock = make_client(empty, empty, empty)
    client.fetch_observed(
        POINTS, utc(2026, 8, 1), utc(2026, 8, 1) + 2 * MAX_WINDOW + timedelta(days=1)
    )

    assert len(clock.sleeps) == 2
    assert all(s == MIN_INTERVAL for s in clock.sleeps)


def test_request_times_are_at_least_one_interval_apart():
    stamps = []

    class RecordingSession(FakeSession):
        def get(self, url, params, timeout):
            stamps.append(clock.now)
            return super().get(url, params, timeout)

    clock = FakeClock()
    session = RecordingSession(*[FakeResponse(200, SINGLE_RUN)] * 3)
    client = WeatherClient(session=session, sleep=clock.sleep, clock=clock.time)
    for _ in range(3):
        client.fetch_run(POINTS, RUN)

    assert all(b - a >= MIN_INTERVAL for a, b in pairwise(stamps))
