"""Tests for the raw store. Local temp folders only, no network."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from helpers import FETCHED, START, make_rows

from flowguard.common.raw_store import BAFU_SCHEMA, WEATHER_SCHEMA, RawStore

RUN = datetime(2026, 10, 25, 6, tzinfo=UTC)


def weather_rows(run_start=RUN, value=1.5):
    return [
        {
            "point": "andermatt",
            "valid_time": run_start + timedelta(hours=h),
            "run_start": run_start,
            "parameter": "precipitation",
            "value": value if h != 2 else None,
            "unit": "mm",
            "fetched_at": FETCHED,
        }
        for h in (1, 2, 3)
    ]


def store_at(tmp_path, day=1):
    clock = lambda: datetime(2026, 10, day, 12, tzinfo=UTC)
    return RawStore(tmp_path, clock=clock)


def parquet_files(tmp_path):
    return sorted(tmp_path.rglob("*.parquet"))


def test_bafu_round_trip(tmp_path):
    rows = make_rows(range(3)) + make_rows([3], release_state="validated")
    rows[1]["value"] = None
    store = RawStore(tmp_path)
    store.write_bafu(rows, "backfill-2087-a")

    df = store.read_bafu()
    assert list(df.columns) == BAFU_SCHEMA.names
    assert len(df) == 4
    assert df["timestamp"].dt.tz is not None
    assert df["timestamp"].iloc[0] == pd.Timestamp(START)
    assert df["fetched_at"].iloc[0] == pd.Timestamp(FETCHED)
    assert pd.isna(df["value"].iloc[1])
    assert pd.isna(df["release_state"].iloc[0])
    assert df["release_state"].iloc[3] == "validated"
    assert df["value"].iloc[2] == 2.0


def test_weather_round_trip_keeps_null_and_run_start(tmp_path):
    store = RawStore(tmp_path)
    store.write_weather(weather_rows(), "run-20261025T06", RUN)

    df = store.read_weather()
    assert list(df.columns) == WEATHER_SCHEMA.names
    assert len(df) == 3
    assert pd.isna(df["value"].iloc[1])
    assert (df["run_start"] == pd.Timestamp(RUN)).all()


def test_second_write_of_a_unit_is_a_new_file(tmp_path):
    store = RawStore(tmp_path)
    first = store.write_bafu(make_rows(range(2)), "backfill-2087-a")
    before = Path(first).read_bytes()

    second = store.write_bafu(make_rows(range(2), value=None), "backfill-2087-a")

    assert first != second
    assert len(parquet_files(tmp_path)) == 2
    assert Path(first).read_bytes() == before
    # Both versions are kept.
    assert len(store.read_bafu()) == 4


def test_units_lists_names_from_the_file_names(tmp_path):
    store = RawStore(tmp_path)
    assert store.units("bafu") == set()
    store.write_bafu(make_rows(range(2)), "backfill-2087-a")
    store.write_bafu(make_rows(range(2)), "backfill-2087-a")
    store.write_bafu(make_rows(range(2)), "hourly-1")
    store.write_weather(weather_rows(), "run-20261025T06", RUN)
    assert store.units("bafu") == {"backfill-2087-a", "hourly-1"}
    assert store.units("weather") == {"run-20261025T06"}


def test_empty_write_counts_as_stored(tmp_path):
    store = RawStore(tmp_path)
    store.write_bafu([], "backfill-2299-a")
    assert store.units("bafu") == {"backfill-2299-a"}
    assert store.read_bafu().empty


def test_no_temporary_file_is_left(tmp_path):
    RawStore(tmp_path).write_bafu(make_rows(range(2)), "u")
    assert not list(tmp_path.rglob("*.tmp"))


def test_unfinished_temporary_file_is_ignored(tmp_path):
    store = RawStore(tmp_path)
    path = store.write_bafu(make_rows(range(2)), "u")
    (tmp_path / "raw" / "bafu" / "ingest_date=2026-10-02").mkdir()
    (tmp_path / "raw" / "bafu" / "ingest_date=2026-10-02" / "x__s__r.parquet.tmp").write_bytes(b"x")
    assert store.units("bafu") == {"u"}
    assert len(store.read_bafu()) == 2
    assert path.endswith(".parquet")


def test_partitions(tmp_path):
    store = store_at(tmp_path, day=7)
    store.write_bafu(make_rows(range(2)), "u")
    store.write_weather(weather_rows(), "run-20261025T06", RUN)
    assert (tmp_path / "raw" / "bafu" / "ingest_date=2026-10-07").is_dir()
    assert (tmp_path / "raw" / "weather" / "run_date=2026-10-25").is_dir()


def test_read_from_timestamp_loses_no_row(tmp_path):
    # Rows are written on the day after their timestamps, like the real job (START is 8 Oct).
    for day in (9, 10, 11):
        rows = make_rows([(day - 9) * 24 + h for h in range(3)])
        store_at(tmp_path, day).write_bafu(rows, f"u{day}")
    store = RawStore(tmp_path)

    from_ts = START + timedelta(hours=24)
    df = store.read_bafu(from_timestamp=from_ts)

    assert len(df) == 6
    assert (df["timestamp"] >= pd.Timestamp(from_ts)).all()
    assert len(store.read_bafu()) == 9


def test_files_of_different_writes_have_the_same_schema(tmp_path):
    import pyarrow.parquet as pq

    store = RawStore(tmp_path)
    store.write_bafu(make_rows(range(2)), "u1")
    store.write_bafu([], "u2")
    schemas = {pq.read_schema(p) for p in parquet_files(tmp_path)}
    assert len(schemas) == 1


def test_unit_name_is_checked(tmp_path):
    with pytest.raises(ValueError):
        RawStore(tmp_path).write_bafu([], "a__b")


def test_relative_base_path_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    RawStore("data").write_bafu(make_rows(range(2)), "u")
    assert (tmp_path / "data" / "raw" / "bafu").is_dir()
