"""Tests for the hourly ingest. Fake clients, no network."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from helpers import make_rows

from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.raw_store import BAFU_SCHEMA, RawStore
from flowguard.common.stations import load_stations
from flowguard.features.bafu_client import BafuError
from flowguard.features.ingest import ingest, new_or_changed
from flowguard.features.weather_client import RunNotAvailableError

NOW = datetime(2026, 10, 26, 14, 30, tzinfo=UTC)
CONFIG = load_pipeline_config()
STATIONS = load_stations()[:2]
LATEST_RUN = datetime(2026, 10, 26, 6, tzinfo=UTC)


class FakeBafu:
    """Delivers 46 hours: the window start to 11:00, because the newest hours are not out yet."""

    def __init__(self, skip=(), changes=None, fail=False):
        self.skip = set(skip)
        self.changes = changes or {}
        self.fail = fail
        self.calls = 0

    def fetch_stations(self, stations, start, end):
        self.calls += 1
        if self.fail:
            raise BafuError("HTTP 403")
        rows = []
        for s in stations:
            hours = [h for h in range(46) if (s.number, h) not in self.skip]
            for row in make_rows(hours, station=s.number, start=start, fetched_at=NOW):
                hour = int((row["timestamp"] - start).total_seconds() // 3600)
                row.update(self.changes.get((s.number, hour), {}))
                rows.append(row)
        return rows


class FakeWeather:
    def __init__(self, missing=()):
        self.calls = []
        self.missing = set(missing)

    def fetch_run(self, points, run_start):
        self.calls.append(run_start)
        if run_start in self.missing:
            raise RunNotAvailableError("run is not available")
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


def run(store, bafu=None, weather=None):
    bafu = bafu or FakeBafu()
    weather = weather or FakeWeather()
    ok = ingest(store, bafu, weather, STATIONS, CONFIG, NOW)
    return ok, bafu, weather


def files(tmp_path):
    return sorted(tmp_path.rglob("*.parquet"))


def test_first_run_writes_one_bafu_file_and_the_weather_runs(tmp_path):
    store = RawStore(tmp_path)
    ok, _, weather = run(store)
    assert ok
    assert len(list((tmp_path / "raw" / "bafu").rglob("*.parquet"))) == 1
    assert len(store.read_bafu()) == 2 * 46
    # Latest allowed run and eight runs before it (48 h).
    assert len(weather.calls) == 9
    assert weather.calls[0] == LATEST_RUN - timedelta(hours=48)
    assert weather.calls[-1] == LATEST_RUN
    assert len(store.units("weather")) == 9


def test_second_run_writes_nothing(tmp_path):
    store = RawStore(tmp_path)
    run(store)
    before = files(tmp_path)

    ok, bafu, weather = run(store)

    assert ok
    assert files(tmp_path) == before
    assert bafu.calls == 1
    assert weather.calls == []


def test_changed_value_and_release_state_are_added_and_the_old_version_stays(tmp_path):
    store = RawStore(tmp_path)
    run(store)
    changes = {
        ("2087", 5): {"value": 99.0},
        ("2491", 6): {"release_state": "validated"},
    }
    run(store, bafu=FakeBafu(changes=changes))

    df = store.read_bafu()
    assert len(files(tmp_path)) == 9 + 2
    assert len(df) == 2 * 46 + 2
    changed = df[(df["station"] == "2087") & (df["value"] == 99.0)]
    assert len(changed) == 1
    # The old version of the same hour is still there.
    start = pd.Timestamp(changed["timestamp"].iloc[0])
    same_hour = df[(df["station"] == "2087") & (df["timestamp"] == start)]
    assert len(same_hour) == 2


def test_stored_weather_run_is_not_fetched_again(tmp_path):
    store = RawStore(tmp_path)
    run(store, weather=FakeWeather(missing=[LATEST_RUN]))
    assert len(store.units("weather")) == 8

    _, _, weather = run(store)
    assert weather.calls == [LATEST_RUN]
    assert len(store.units("weather")) == 9


def test_bafu_failure_does_not_stop_weather(tmp_path):
    store = RawStore(tmp_path)
    ok, _, weather = run(store, bafu=FakeBafu(fail=True))
    assert not ok
    assert len(weather.calls) == 9
    assert store.read_bafu().empty


def test_gap_report_lists_the_missing_hour(tmp_path, capsys):
    store = RawStore(tmp_path)
    run(store, bafu=FakeBafu(skip=[("2087", 10)]))
    out = capsys.readouterr().out
    # Window starts 24.10. 14:00, hour 10 is 25.10. 00:00.
    assert "2087 W: 10-25 00:00 to 10-25 00:00, 1 h" in out
    assert "2491" not in out


def test_gap_report_without_gaps(tmp_path, capsys):
    run(RawStore(tmp_path))
    assert "no gaps" in capsys.readouterr().out


def test_gap_report_without_data(tmp_path, capsys):
    run(RawStore(tmp_path), bafu=FakeBafu(fail=True))
    assert "no BAFU data" in capsys.readouterr().out


def test_new_or_changed_treats_empty_values_correctly():
    row = make_rows([0], value=None)[0]
    empty = pd.DataFrame(columns=BAFU_SCHEMA.names)
    assert new_or_changed([row], empty) == [row]

    stored = pd.DataFrame([row])
    assert new_or_changed([row], stored) == []
    assert new_or_changed([{**row, "value": 1.0}], stored) != []


def run_main(monkeypatch, argv, env=None):
    """Run the command line with the real work replaced; returns the store path it was given."""
    from flowguard.features import ingest as module

    paths = []
    monkeypatch.setattr(module, "RawStore", lambda path: paths.append(path))
    monkeypatch.setattr(module, "BafuClient", lambda: None)
    monkeypatch.setattr(module, "WeatherClient", lambda: None)
    monkeypatch.setattr(module, "ingest", lambda *args: True)
    monkeypatch.delenv("FLOWGUARD_STORE", raising=False)
    if env:
        monkeypatch.setenv("FLOWGUARD_STORE", env)
    with pytest.raises(SystemExit) as stop:
        module.main(argv)
    assert stop.value.code == 0
    return paths[0]


def test_store_comes_from_the_environment_and_the_flag_wins(monkeypatch):
    assert run_main(monkeypatch, [], env="gs://bucket") == "gs://bucket"
    assert run_main(monkeypatch, ["--store", "other"], env="gs://bucket") == "other"
    assert run_main(monkeypatch, []) == "data"
