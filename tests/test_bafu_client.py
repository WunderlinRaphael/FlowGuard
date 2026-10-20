"""Tests for the BAFU client. A fake session replaces the network, so no real calls are made."""

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
import requests

from flowguard.common.stations import Station
from flowguard.features.bafu_client import (
    BACKOFF,
    MAX_ATTEMPTS,
    MAX_WINDOW,
    MIN_INTERVAL,
    BafuClient,
    BafuError,
    RowLimitError,
)

RECORDED = Path(__file__).parent / "fixtures" / "bafu_hourly_2056.json"
START = datetime(2026, 10, 8, 6, tzinfo=UTC)
END = datetime(2026, 10, 8, 9, tzinfo=UTC)

SEEDORF = Station("2056", "Seedorf", "Reuss", "target", ("Q", "W", "WT"), "2087", "2491", ())
EW_ALTDORF = Station("2492", "EW Altdorf", "Dorfbachableitung", "input", ("Q",), None, None, ())


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
    """Returns the queued responses in order and records every request body."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, json, timeout):
        self.requests.append(json)
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
    return BafuClient(session=session, sleep=clock.sleep, clock=clock.time), session, clock


def data(records):
    return FakeResponse(200, {"data": {"water": {"observations": {"data_1hour_mean": records}}}})


def recorded():
    return FakeResponse(200, json.loads(RECORDED.read_text(encoding="utf-8")))


def test_normal_response_is_mapped_to_raw_rows():
    client, session, _ = make_client(recorded())

    rows = client.fetch_station(SEEDORF, START, END)

    assert len(rows) == 9
    first = rows[0]
    assert first["station"] == "2056"
    assert first["timestamp"] == datetime(2026, 10, 8, 6, tzinfo=UTC)
    assert first["parameter"] == "Q"
    assert first["value"] == 17.468
    assert first["unit"] == "m3/s"
    assert first["release_state"] is None
    assert first["fetched_at"].tzinfo is not None
    assert {r["parameter"] for r in rows} == {"Q", "W", "WT"}
    wt = next(r for r in rows if r["parameter"] == "WT")
    assert wt["unit"] == "°C"

    variables = session.requests[0]["variables"]
    assert variables == {
        "station": "2056",
        "parameters": ["Q", "W", "WT"],
        "from": "2026-10-08T06:00:00Z",
        "to": "2026-10-08T09:00:00Z",
    }


def test_release_state_is_kept():
    record = {
        "timestamp": "2025-10-09T00:00:00Z",
        "parameterName": "Q",
        "value": 1.5,
        "unitSymbol": "m3/s",
        "releaseState": "2",
    }
    client, _, _ = make_client(data([record]))

    rows = client.fetch_station(EW_ALTDORF, START, END)

    assert rows[0]["release_state"] == "2"


def test_only_parameters_of_the_station_are_requested():
    client, session, _ = make_client(data([]))

    client.fetch_station(EW_ALTDORF, START, END)

    assert session.requests[0]["variables"]["parameters"] == ["Q"]


def test_station_without_parameters_sends_no_request():
    empty = Station("9999", "Empty", "River", "observe_only", (), None, None, ())
    client, session, _ = make_client()

    assert client.fetch_station(empty, START, END) == []
    assert session.requests == []


def test_errors_on_http_200_raise():
    body = {"data": None, "errors": [{"message": "Validation error of type FieldUndefined"}]}
    client, _, _ = make_client(FakeResponse(200, body))

    with pytest.raises(BafuError, match="FieldUndefined"):
        client.fetch_station(SEEDORF, START, END)


def test_row_limit_error_is_recognised():
    # Message as returned by BAFU on 09.10.2026 for a 5-month query.
    message = (
        "Query returned more than 10000 rows. Please narrow your query using filters "
        "or specify a limit."
    )
    body = {
        "data": {"water": {"observations": {"data_1hour_mean": None}}},
        "errors": [{"message": message}],
    }
    client, session, _ = make_client(FakeResponse(200, body))

    with pytest.raises(RowLimitError):
        client.fetch_station(SEEDORF, START, END)
    # Not retried: the same query would be rejected again.
    assert len(session.requests) == 1


def test_empty_window_returns_no_rows():
    client, _, _ = make_client(data([]))

    assert client.fetch_station(SEEDORF, START, END) == []


def test_long_range_is_split_into_windows_of_at_most_30_days():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=75)
    client, session, _ = make_client(data([]), data([]), data([]))

    client.fetch_station(SEEDORF, start, end)

    windows = [
        (
            datetime.fromisoformat(r["variables"]["from"]),
            datetime.fromisoformat(r["variables"]["to"]),
        )
        for r in session.requests
    ]
    assert len(windows) == 3
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (_, a_end), (b_start, _) in pairwise(windows):
        assert a_end == b_start
    assert all(w_end - w_start <= MAX_WINDOW for w_start, w_end in windows)


def test_retries_after_rate_limit_and_server_error():
    client, session, clock = make_client(FakeResponse(403), FakeResponse(503), recorded())

    rows = client.fetch_station(SEEDORF, START, END)

    assert len(rows) == 9
    assert len(session.requests) == 3
    assert BACKOFF in clock.sleeps
    assert BACKOFF * 2 in clock.sleeps


def test_retries_after_connection_error():
    client, session, _ = make_client(requests.ConnectionError("reset"), recorded())

    rows = client.fetch_station(SEEDORF, START, END)

    assert len(rows) == 9
    assert len(session.requests) == 2


def test_gives_up_after_max_attempts():
    client, session, _ = make_client(*[FakeResponse(503)] * MAX_ATTEMPTS)

    with pytest.raises(BafuError, match="Giving up"):
        client.fetch_station(SEEDORF, START, END)
    assert len(session.requests) == MAX_ATTEMPTS


def test_other_http_errors_are_not_retried():
    client, session, _ = make_client(FakeResponse(400))

    with pytest.raises(requests.HTTPError):
        client.fetch_station(SEEDORF, START, END)
    assert len(session.requests) == 1


def test_requests_are_spaced_by_min_interval():
    client, _, clock = make_client(data([]), data([]))

    client.fetch_stations([SEEDORF, EW_ALTDORF], START, END)

    # The second request waits, because the fake clock did not move in between.
    assert clock.sleeps == [MIN_INTERVAL]


def test_naive_datetime_is_rejected():
    client, _, _ = make_client()

    with pytest.raises(ValueError, match="UTC"):
        naive = datetime(2026, 10, 8)  # noqa: DTZ001 (deliberately without timezone)
        client.fetch_station(SEEDORF, naive, END)
