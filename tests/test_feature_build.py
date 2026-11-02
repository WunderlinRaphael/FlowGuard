"""Tests for the feature build: storage layout, manifest, incremental build.

A small synthetic raw store (10 weeks of BAFU data and weather runs), no network. The data
holds the cases that matter for an incremental build: a flat run that starts long before the
rebuilt months, a spike, a gap, a missing weather run and stations with different spans.
"""

import json
import shutil
import zlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import pytest

from flowguard.common import raw_store
from flowguard.common.feature_table import build_feature_table, feature_table_columns
from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.raw_store import RawStore
from flowguard.common.stations import load_stations
from flowguard.common.weather_features import weather_columns
from flowguard.features import build as build_module
from flowguard.features.build import build, lookback, main
from flowguard.features.feature_store import FeatureStore, feature_schema
from flowguard.features.hourly_grid import build_hourly_grid
from flowguard.features.weather_client import VARIABLES

START = datetime(2026, 9, 1, tzinfo=UTC)
HOURS = 70 * 24  # up to 2026-11-10 23:00
FETCHED = datetime(2026, 11, 11, tzinfo=UTC)
FIRST_RUN = datetime(2026, 9, 25, tzinfo=UTC)
MISSING_RUN = datetime(2026, 10, 30, 12, tzinfo=UTC)
CLOCK = datetime(2026, 11, 11, 12, tzinfo=UTC)
# (station, parameter) -> (first hour, last hour + 1, base value, step). W in m a.s.l., Q in m3/s.
SERIES = {
    ("2087", "W"): (0, HOURS - 10, 1426.8, 0.002),
    ("2087", "Q"): (0, HOURS - 10, 20.0, 0.05),
    ("2491", "W"): (0, HOURS, 486.0, 0.002),
    ("2491", "Q"): (0, HOURS, 10.0, 0.05),
    ("2056", "W"): (100, HOURS, 438.0, 0.002),
    ("2056", "Q"): (100, HOURS, 50.0, 0.05),
    ("2056", "WT"): (100, HOURS, 8.0, 0.01),
    ("2492", "Q"): (0, HOURS, 1.0, 0.005),
    ("2649", "Q"): (0, HOURS, 5.0, 0.02),
    ("2299", "W"): (0, HOURS, 1020.0, 0.002),
    ("2299", "Q"): (0, HOURS, 10.0, 0.05),
}
# Bürglen W stands still from 2026-10-03 to 2026-11-03: a flat run (336 h are enough to flag
# it) that starts before the window of an incremental build for November.
FLAT = (32 * 24, 63 * 24)
SPIKE_HOUR = 1000  # Seedorf W
GAP = range(800, 806)  # Andermatt W


@pytest.fixture
def stations():
    return load_stations()


@pytest.fixture
def config():
    # weather_start 2026-10-01 -> first_weather_origin 2026-10-04 00:00 UTC
    return replace(load_pipeline_config(), weather_start=date(2026, 10, 1))


def bafu_rows(station, parameter):
    first, stop, base, step = SERIES[(station, parameter)]
    rng = np.random.default_rng(zlib.crc32(f"{station}{parameter}".encode()))
    values = base + np.cumsum(rng.normal(0, step, HOURS))
    if (station, parameter) == ("2491", "W"):
        values[FLAT[0] : FLAT[1]] = values[FLAT[0]]
    if (station, parameter) == ("2056", "W"):
        values[SPIKE_HOUR] += 2.0
    return [
        {
            "station": station,
            "timestamp": START + timedelta(hours=h),
            "parameter": parameter,
            "value": float(values[h]),
            "unit": "m",
            "release_state": None,
            "fetched_at": FETCHED,
        }
        for h in range(first, stop)
        if not ((station, parameter) == ("2087", "W") and h in GAP)
    ]


def weather_rows(run_start, points):
    rows = []
    run = int((run_start - FIRST_RUN) / timedelta(hours=6))
    for p, name in enumerate(points):
        for v, parameter in enumerate(VARIABLES):
            for lead in range(1, 48):
                rows.append(
                    {
                        "point": name,
                        "valid_time": run_start + timedelta(hours=lead),
                        "run_start": run_start,
                        "parameter": parameter,
                        "value": float((run * 7 + lead * 3 + p * 5 + v) % 11) / 2,
                        "unit": "x",
                        "fetched_at": FETCHED,
                    }
                )
    return rows


def fill(store, config, with_weather=True):
    """Write the synthetic raw data into a RawStore."""
    for station, parameter in SERIES:
        store.write_bafu(bafu_rows(station, parameter), f"{station}-{parameter}")
    if not with_weather:
        return
    names = [p.name for p in config.weather_points]
    run = FIRST_RUN
    while run < START + timedelta(hours=HOURS):
        if run != MISSING_RUN:
            store.write_weather(weather_rows(run, names), f"run-{run:%Y%m%dT%H}", run)
        run += timedelta(hours=6)


@pytest.fixture(scope="module")
def raw_template(tmp_path_factory):
    """A raw store on disk, copied for every test (the copies get the features written)."""
    path = tmp_path_factory.mktemp("raw_template")
    config = replace(load_pipeline_config(), weather_start=date(2026, 10, 1))
    fill(RawStore(path, clock=lambda: CLOCK), config)
    return path


@pytest.fixture
def raw(tmp_path, raw_template):
    shutil.copytree(raw_template, tmp_path / "store")
    return RawStore(tmp_path / "store")


def snapshot(raw):
    """Bytes of every month file (the manifest is left out), by relative path."""
    base = f"{raw.root}/features"
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in sorted(Path(base).rglob("features.parquet"))
    }


def manifest(raw):
    return json.loads((Path(raw.root) / "features/v1/_manifest.json").read_text())


def without_built_at(m):
    return {k: v for k, v in m.items() if k != "built_at"}


def read_table(raw, stations, config):
    store = FeatureStore(raw.fs, raw.root, config.feature_version)
    frames = [
        store.read_month(s.number, month)
        for s in stations
        if s.role == "target"
        for month in store.months(s.number)
    ]
    table = pd.concat(frames, ignore_index=True)
    table["t"] = table["t"].astype("datetime64[ns, UTC]")
    return table.sort_values(["station_id", "t"]).reset_index(drop=True)


def run(raw, config, stations, **kwargs):
    return build(raw, config, stations, clock=lambda: CLOCK, **kwargs)


def remove_months(raw, month):
    for path in Path(raw.root).rglob(f"month={month}"):
        shutil.rmtree(path)


# Round trip


def test_round_trip_local(raw, config, stations):
    result = run(raw, config, stations)
    table = read_table(raw, stations, config)

    grid = build_hourly_grid(raw.read_bafu().to_dict("records")).grid
    expected = build_feature_table(grid, raw.read_weather(), stations, config)
    expected = expected.sort_values(["station_id", "t"]).reset_index(drop=True)
    # release_state is empty in the synthetic data: all missing in memory, a string type on disk.
    pd.testing.assert_frame_equal(table, expected, check_dtype=False)
    assert list(table.columns) == feature_table_columns(stations, config)
    assert len(table.columns) == 156
    assert result.written == 9 and result.unchanged == 0  # 3 stations x 3 months


def test_stored_schema_is_explicit(raw, config, stations):
    run(raw, config, stations)
    store = FeatureStore(raw.fs, raw.root, "v1")
    schema = pq.read_schema(store.month_path("2087", "2026-10"))
    assert schema.equals(feature_schema(stations, config))
    assert str(schema.field("t").type) == "timestamp[us, tz=UTC]"
    assert str(schema.field("y_delta_cm").type) == "double"


@pytest.fixture
def fake_gcs(tmp_path, monkeypatch):
    def from_uri(uri):
        return pafs.SubTreeFileSystem(str(tmp_path / "bucket"), pafs.LocalFileSystem()), "prefix"

    (tmp_path / "bucket" / "prefix").mkdir(parents=True)
    monkeypatch.setattr(raw_store, "filesystem_from_uri", from_uri)


def test_round_trip_on_a_gs_uri(tmp_path, fake_gcs, config, stations):
    raw = RawStore("gs://bucket/prefix", clock=lambda: CLOCK)
    fill(raw, config)
    result = run(raw, config, stations)

    files = sorted(
        p.relative_to(tmp_path / "bucket/prefix").as_posix()
        for p in (tmp_path / "bucket/prefix/features").rglob("*")
        if p.is_file()
    )
    assert "features/v1/_manifest.json" in files
    assert "features/v1/station=2087/month=2026-10/features.parquet" in files
    assert not any(f.endswith(".tmp") for f in files)
    assert result.written == 9

    table = read_table(raw, stations, config)
    assert len(table.columns) == 156
    again = run(raw, config, stations)
    assert (again.written, again.unchanged) == (0, 9)


# Rerun and clock


def test_running_twice_writes_nothing(raw, config, stations):
    run(raw, config, stations)
    first = snapshot(raw)
    first_manifest = manifest(raw)
    mtimes = {p: p.stat().st_mtime_ns for p in Path(raw.root).rglob("features.parquet")}

    later = build(raw, config, stations, clock=lambda: CLOCK + timedelta(days=1))
    assert (later.written, later.unchanged) == (0, 9)
    assert snapshot(raw) == first
    assert {p: p.stat().st_mtime_ns for p in mtimes} == mtimes
    # Only the build time changes.
    assert manifest(raw)["built_at"] != first_manifest["built_at"]
    assert without_built_at(manifest(raw)) == without_built_at(first_manifest)


def test_content_does_not_depend_on_the_clock(tmp_path, raw_template, config, stations):
    shutil.copytree(raw_template, tmp_path / "a")
    shutil.copytree(raw_template, tmp_path / "b")
    a, b = RawStore(tmp_path / "a"), RawStore(tmp_path / "b")
    build(a, config, stations, clock=lambda: datetime(2026, 11, 11, tzinfo=UTC))
    build(b, config, stations, clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    assert snapshot(a) == snapshot(b)
    assert without_built_at(manifest(a)) == without_built_at(manifest(b))


# Incremental build


@pytest.mark.parametrize("since", [date(2026, 11, 1), date(2026, 11, 5), date(2026, 10, 15)])
def test_incremental_equals_full(raw, config, stations, since):
    run(raw, config, stations)
    full = snapshot(raw)
    full_manifest = manifest(raw)

    first = f"{since:%Y-%m}"
    for key in full:
        if key.split("/")[2] >= f"month={first}":
            (Path(raw.root) / "features" / key).unlink()
    result = run(raw, config, stations, since=since)

    assert result.written > 0
    assert snapshot(raw) == full
    assert without_built_at(manifest(raw)) == without_built_at(full_manifest)


def test_incremental_only_touches_months_from_since(raw, config, stations):
    run(raw, config, stations)
    result = run(raw, config, stations, since=date(2026, 11, 3))
    # November only: 3 stations, all identical to the full build.
    assert (result.written, result.unchanged) == (0, 3)


def test_incremental_needs_the_longer_lookback(raw, config, stations, monkeypatch):
    # Guards the lookback: with only 8 days, the flat run that began in October is not
    # recognised, and November differs from the full build.
    assert lookback(config.cleaning) == timedelta(days=22)
    run(raw, config, stations)
    full = snapshot(raw)
    remove_months(raw, "2026-11")

    monkeypatch.setattr(build_module, "lookback", lambda cleaning: timedelta(days=8))
    run(raw, config, stations, since=date(2026, 11, 1))
    short = snapshot(raw)
    assert short != full
    assert (
        short["v1/station=2491/month=2026-11/features.parquet"]
        != full["v1/station=2491/month=2026-11/features.parquet"]
    )


# Manifest


def test_manifest_fields(raw, config, stations, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    run(raw, config, stations)
    m = manifest(raw)

    assert m["feature_version"] == "v1"
    assert m["built_at"] == "2026-11-11T12:00:00Z"
    assert m["code_version"] == "abc123"
    assert m["newest_fetched_at"] == {
        "bafu": "2026-11-11T00:00:00Z",
        "weather": "2026-11-11T00:00:00Z",
    }
    assert set(m["stations"]) == {"2087", "2491", "2056"}
    for entry in m["stations"].values():
        assert set(entry) == {"rows", "rows_with_target", "rows_with_weather", "first_t", "last_t"}
        assert 0 < entry["rows_with_weather"] < entry["rows"]
        assert 0 < entry["rows_with_target"] < entry["rows"]
    # Totals are the sums of the months.
    for number, months in m["months"].items():
        assert m["stations"][number]["rows"] == sum(v["rows"] for v in months.values())


def test_code_version_is_local_without_github_sha(raw, config, stations, monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    run(raw, config, stations)
    assert manifest(raw)["code_version"] == "local"


def test_manifest_counts_the_flags_of_the_full_check(raw, config, stations):
    run(raw, config, stations)
    flags = manifest(raw)["cleaning_flags"]
    assert flags["2056/W"]["spike"] == 1
    # The whole flat run is flagged in the full check, not only the part after 336 hours.
    assert flags["2491/W"]["flat"] == FLAT[1] - FLAT[0]
    # A series without any flag is not listed.
    assert "2087/Q" not in flags


def test_each_station_runs_over_its_own_span(raw, config, stations):
    run(raw, config, stations)
    m = manifest(raw)["stations"]
    iso = lambda h: (START + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (m["2087"]["first_t"], m["2087"]["last_t"]) == (iso(0), iso(HOURS - 11))
    assert (m["2491"]["first_t"], m["2491"]["last_t"]) == (iso(0), iso(HOURS - 1))
    assert (m["2056"]["first_t"], m["2056"]["last_t"]) == (iso(100), iso(HOURS - 1))


# Content


def test_weather_is_missing_before_the_first_weather_origin(raw, config, stations):
    run(raw, config, stations)
    table = read_table(raw, stations, config)
    wx = weather_columns(config.weather_points)
    first = pd.Timestamp(config.first_weather_origin)

    before = table[table["t"] < first]
    assert len(before) > 0 and before[wx].isna().all().all()
    row = table[table["t"] == first]
    assert len(row) == 3  # all three stations
    assert row[wx].notna().all().all()


def test_every_station_gets_the_columns_of_all_weather_points(raw, config, stations):
    run(raw, config, stations)
    table = read_table(raw, stations, config)
    assert {c.split("_")[1] for c in weather_columns(config.weather_points)[:-1]} == {
        "andermatt",
        "meiental",
        "schaechental",
    }
    t = pd.Timestamp(datetime(2026, 10, 20, 12, tzinfo=UTC))
    rows = table[table["t"] == t].set_index("station_id")
    wx = weather_columns(config.weather_points)
    assert rows[wx].notna().all().all()
    # Weather does not depend on the station, so the rows agree.
    assert (rows.loc["2087", wx] == rows.loc["2056", wx]).all()


def test_the_newest_rows_stay_without_target(raw, config, stations):
    run(raw, config, stations)
    table = read_table(raw, stations, config)
    seedorf = table[table["station_id"] == "2056"]
    last = seedorf.tail(24)
    assert last["y_delta_cm"].isna().all()
    assert seedorf["t"].iloc[-1] == pd.Timestamp(START + timedelta(hours=HOURS - 1))
    assert seedorf["y_delta_cm"].iloc[:-24].notna().sum() > 0


def test_no_weather_in_the_store_still_builds(tmp_path, config, stations):
    raw = RawStore(tmp_path, clock=lambda: CLOCK)
    fill(raw, config, with_weather=False)
    result = run(raw, config, stations)
    table = read_table(raw, stations, config)
    assert len(table.columns) == 156
    assert table[weather_columns(config.weather_points)].isna().all().all()
    assert result.manifest["newest_fetched_at"]["weather"] is None


def test_empty_store_writes_no_month_file(tmp_path, config, stations):
    result = run(RawStore(tmp_path), config, stations)
    assert result.manifest["stations"] == {}
    assert result.written == 0
    assert "no rows" in result.report


def test_report_lists_stations_flags_and_files(raw, config, stations):
    report = run(raw, config, stations).report
    assert "full build" in report
    assert "2491:" in report and "with target" in report
    assert "flags 2056/W: range 0, spike 1, flat 0" in report
    assert "month files: 9 written, 0 unchanged" in report


# Command line


def test_main_builds_with_store_and_since(raw, capsys):
    with pytest.raises(SystemExit) as full:
        main(["--store", raw.root])
    assert full.value.code == 0
    with pytest.raises(SystemExit) as incremental:
        main(["--store", raw.root, "--since", "2026-11-01"])
    assert incremental.value.code == 0
    out = capsys.readouterr().out
    assert "(full build)" in out and "(since 2026-11-01)" in out
    assert "0 written, 3 unchanged" in out


def test_main_exits_with_1_on_an_empty_store(tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        main(["--store", str(tmp_path)])
    assert exit_info.value.code == 1
