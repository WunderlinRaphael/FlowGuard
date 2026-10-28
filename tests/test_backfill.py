"""Tests for the backfill. Fake clients count the calls, no network."""

import logging
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

from helpers import make_rows

from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.raw_store import RawStore
from flowguard.common.stations import load_stations
from flowguard.features.backfill import (
    backfill,
    bafu_unit,
    bafu_windows,
    latest_run,
    weather_runs,
)
from flowguard.features.bafu_client import BafuError
from flowguard.features.weather_client import RunNotAvailableError, WeatherError

NOW = datetime(2026, 10, 26, 14, 30, tzinfo=UTC)
CONFIG = load_pipeline_config()
STATIONS = load_stations()[:3]
BAFU_START = date(2026, 9, 1)
WEATHER_START = date(2026, 10, 25)


class FakeBafu:
    def __init__(self, fail_on=None, empty=False):
        self.calls = []
        self.fail_on = fail_on
        self.empty = empty

    def fetch_station(self, station, start, end):
        self.calls.append((station.number, start, end))
        if self.fail_on == len(self.calls):
            raise BafuError("HTTP 403")
        if self.empty:
            return []
        return make_rows([0, 1], station=station.number, start=start)


class FakeWeather:
    def __init__(self, missing=(), fail_on=None):
        self.calls = []
        self.missing = set(missing)
        self.fail_on = fail_on

    def fetch_run(self, points, run_start):
        self.calls.append(run_start)
        if run_start in self.missing:
            raise RunNotAvailableError("run is not available")
        if self.fail_on == len(self.calls):
            raise WeatherError("Giving up after 5 attempts: HTTP 429")
        return [
            {
                "point": p.name,
                "valid_time": run_start + timedelta(hours=1),
                "run_start": run_start,
                "parameter": "precipitation",
                "value": 0.5,
                "unit": "mm",
                "fetched_at": NOW,
            }
            for p in points
        ]


def run(tmp_path, bafu=None, weather=None, **kwargs):
    store = RawStore(tmp_path)
    bafu = bafu or FakeBafu()
    weather = weather or FakeWeather()
    kwargs.setdefault("bafu_start", BAFU_START)
    kwargs.setdefault("weather_start", WEATHER_START)
    ok = backfill(store, bafu, weather, STATIONS, CONFIG, NOW, **kwargs)
    return ok, store, bafu, weather


def test_bafu_windows_are_30_days_and_the_last_is_shorter():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 10, 26, tzinfo=UTC)
    assert bafu_windows(start, end) == [
        (start, datetime(2026, 10, 1, tzinfo=UTC)),
        (datetime(2026, 10, 1, tzinfo=UTC), end),
    ]
    assert bafu_windows(end, end) == []


def test_bafu_unit_name_contains_station_and_both_dates():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    assert bafu_unit(STATIONS[0], start, start + timedelta(days=30)) == (
        "backfill-2087-20260901-20261001"
    )


def test_weather_runs_start_at_weather_start_in_6_hour_steps():
    latest = latest_run(NOW, CONFIG)
    runs = weather_runs(WEATHER_START, latest)
    assert runs[0] == datetime(2026, 10, 25, 0, tzinfo=UTC)
    assert runs[-1] == latest
    assert len(runs) == 6
    assert {b - a for a, b in pairwise(runs)} == {timedelta(hours=6)}


def test_default_plan_starts_at_weather_start_with_no_earlier_run():
    runs = weather_runs(CONFIG.weather_start, latest_run(NOW, CONFIG))
    assert runs[0] == datetime(2024, 8, 7, 0, tzinfo=UTC)
    # The first origin with weather features is three days later.
    assert CONFIG.first_weather_origin - runs[0] == timedelta(hours=72)


def test_latest_run_is_old_enough_at_the_as_of_time():
    # The as-of time is the full hour of now; the run must be at least 7 h older.
    assert latest_run(NOW, CONFIG) == datetime(2026, 10, 26, 6, tzinfo=UTC)
    just_before = datetime(2026, 10, 26, 12, 59, tzinfo=UTC)
    assert latest_run(just_before, CONFIG) == datetime(2026, 10, 26, 0, tzinfo=UTC)
    just_after = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)
    assert latest_run(just_after, CONFIG) == datetime(2026, 10, 26, 6, tzinfo=UTC)


def test_fresh_run_makes_one_request_per_unit(tmp_path):
    ok, store, bafu, weather = run(tmp_path)
    assert ok
    # 3 stations x 2 windows, 6 runs.
    assert len(bafu.calls) == 6
    assert len(weather.calls) == 6
    assert len(store.units("bafu")) == 6
    assert len(store.units("weather")) == 6
    # The last window ends at the start of today, not at "now".
    assert max(end for _, _, end in bafu.calls) == datetime(2026, 10, 26, tzinfo=UTC)


def test_second_run_makes_no_request(tmp_path):
    run(tmp_path)
    ok, _, bafu, weather = run(tmp_path)
    assert ok
    assert bafu.calls == []
    assert weather.calls == []


def test_limit_then_resume_fetches_only_the_rest(tmp_path):
    _, store, bafu, weather = run(tmp_path, limit=2)
    assert len(bafu.calls) == 2
    assert len(weather.calls) == 2

    _, store, bafu2, weather2 = run(tmp_path)
    assert len(bafu2.calls) == 4
    assert len(weather2.calls) == 4
    assert not set(weather.calls) & set(weather2.calls)
    assert len(store.units("weather")) == 6


def test_source_selects_what_is_fetched(tmp_path):
    _, _, bafu, weather = run(tmp_path, sources=("weather",))
    assert bafu.calls == []
    assert len(weather.calls) == 6


def test_dry_run_prints_counts_and_does_nothing(tmp_path, capsys):
    ok, store, bafu, weather = run(tmp_path, dry_run=True)
    assert ok
    assert bafu.calls == [] and weather.calls == []
    assert store.units("bafu") == set() and store.units("weather") == set()
    out = capsys.readouterr().out
    assert "bafu: 6 units, 0 stored, 6 to do, 6 requests" in out
    assert "weather: 6 units, 0 stored, 6 to do, 6 requests" in out


def test_dry_run_counts_stored_units(tmp_path, capsys):
    run(tmp_path, limit=2)
    capsys.readouterr()
    run(tmp_path, dry_run=True)
    out = capsys.readouterr().out
    assert "bafu: 6 units, 2 stored, 4 to do" in out
    assert "weather: 6 units, 2 stored, 4 to do" in out


def test_empty_window_counts_as_stored(tmp_path):
    _, store, _, _ = run(tmp_path, bafu=FakeBafu(empty=True), sources=("bafu",))
    assert len(store.units("bafu")) == 6
    _, _, bafu2, _ = run(tmp_path, sources=("bafu",))
    assert bafu2.calls == []


def test_missing_run_is_skipped_and_tried_again(tmp_path, caplog):
    first = datetime(2026, 10, 25, 6, tzinfo=UTC)
    with caplog.at_level(logging.WARNING):
        ok, store, _, _ = run(tmp_path, weather=FakeWeather(missing=[first]))
    assert ok
    assert len(store.units("weather")) == 5
    assert "not available" in caplog.text

    _, store, _, weather2 = run(tmp_path)
    assert weather2.calls == [first]
    assert len(store.units("weather")) == 6


def test_fetch_error_stops_and_keeps_stored_units(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        ok, store, _, weather = run(tmp_path, weather=FakeWeather(fail_on=3))
    assert not ok
    # BAFU came first and is complete; weather stopped at the third request.
    assert len(store.units("bafu")) == 6
    assert len(store.units("weather")) == 2
    assert len(weather.calls) == 3
    assert "run the command again" in caplog.text


def test_bafu_error_stops_before_weather(tmp_path):
    ok, store, _, weather = run(tmp_path, bafu=FakeBafu(fail_on=2))
    assert not ok
    assert len(store.units("bafu")) == 1
    assert weather.calls == []


def test_start_after_end_means_nothing_to_do(tmp_path):
    ok, _, bafu, _ = run(tmp_path, bafu_start=date(2026, 12, 1), sources=("bafu",))
    assert ok
    assert bafu.calls == []


def test_store_comes_from_the_environment_and_the_flag_wins(monkeypatch):
    import pytest

    from flowguard.features import backfill as module

    paths = []
    monkeypatch.setattr(module, "RawStore", lambda path: paths.append(path))
    monkeypatch.setattr(module, "BafuClient", lambda: None)
    monkeypatch.setattr(module, "WeatherClient", lambda: None)
    monkeypatch.setattr(module, "backfill", lambda *args, **kwargs: True)
    for env, argv in [("gs://bucket", []), ("gs://bucket", ["--store", "other"]), (None, [])]:
        monkeypatch.delenv("FLOWGUARD_STORE", raising=False)
        if env:
            monkeypatch.setenv("FLOWGUARD_STORE", env)
        with pytest.raises(SystemExit):
            module.main(argv)
    assert paths == ["gs://bucket", "other", "data"]
