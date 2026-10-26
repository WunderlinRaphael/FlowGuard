"""Append-only raw store for BAFU rows and weather rows (Parquet).

Layout under the base path:
    raw/bafu/ingest_date=YYYY-MM-DD/<unit>__<fetch stamp>__<random>.parquet
    raw/weather/run_date=YYYY-MM-DD/<unit>__<fetch stamp>__<random>.parquet

A unit names what was fetched (for example one station and one 30-day window, or one
model run). It is part of the file name, so "is this unit stored?" is a directory listing.
Files are never overwritten or deleted: a second write of the same unit is a new file.
The base path is a local folder or a gs:// URI; only the filesystem object differs.
"""

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.fs as pafs
import pyarrow.parquet as pq

TIMESTAMP = pa.timestamp("us", tz="UTC")

# Explicit schemas: with inferred types an all-empty column (for example release_state of
# fresh values) would be typed "null" and the files would not match each other.
BAFU_SCHEMA = pa.schema(
    [
        ("station", pa.string()),
        ("timestamp", TIMESTAMP),
        ("parameter", pa.string()),
        ("value", pa.float64()),
        ("unit", pa.string()),
        ("release_state", pa.string()),
        ("fetched_at", TIMESTAMP),
    ]
)
WEATHER_SCHEMA = pa.schema(
    [
        ("point", pa.string()),
        ("valid_time", TIMESTAMP),
        ("run_start", TIMESTAMP),
        ("parameter", pa.string()),
        ("value", pa.float64()),
        ("unit", pa.string()),
        ("fetched_at", TIMESTAMP),
    ]
)
SCHEMAS = {"bafu": BAFU_SCHEMA, "weather": WEATHER_SCHEMA}
PARTITION_KEY = {"bafu": "ingest_date", "weather": "run_date"}
SEPARATOR = "__"


def now_utc():
    return datetime.now(UTC)


class RawStore:
    def __init__(self, base, filesystem=None, clock=now_utc):
        # clock can be replaced in tests, so file names and partitions are predictable.
        self.clock = clock
        if filesystem is not None:
            self.fs, self.root = filesystem, str(base)
        elif "://" in str(base):
            self.fs, self.root = pafs.FileSystem.from_uri(str(base))
        else:
            # from_uri does not accept relative paths, so local folders are resolved here.
            self.fs, self.root = pafs.LocalFileSystem(), str(Path(base).resolve())

    def write_bafu(self, rows, unit):
        """Store BAFU rows as a new file in today's ingest_date partition."""
        return self._write("bafu", self.clock().date(), unit, rows)

    def write_weather(self, rows, unit, run_start):
        """Store the weather rows of one model run in the partition of the run's date."""
        return self._write("weather", run_start.date(), unit, rows)

    def units(self, kind):
        """The units that have at least one file."""
        return {path.rsplit("/", 1)[-1].split(SEPARATOR)[0] for path, _ in self._files(kind)}

    def read_bafu(self, from_timestamp=None):
        """All stored BAFU rows, every version. With from_timestamp: rows from that hour on."""
        # A row is fetched at or after its timestamp, so it never sits in an earlier
        # ingest_date partition. Older partitions can be skipped without losing a row.
        from_date = from_timestamp.astimezone(UTC).date() if from_timestamp is not None else None
        df = self._read("bafu", from_date)
        if from_timestamp is not None:
            df = df[df["timestamp"] >= from_timestamp].reset_index(drop=True)
        return df

    def read_weather(self):
        return self._read("weather")

    def _write(self, kind, partition_date, unit, rows):
        if SEPARATOR in unit or "/" in unit:
            raise ValueError(f"Invalid unit name {unit!r}")
        table = pa.Table.from_pylist(list(rows), schema=SCHEMAS[kind])
        folder = f"{self.root}/raw/{kind}/{PARTITION_KEY[kind]}={partition_date.isoformat()}"
        stamp = self.clock().strftime("%Y%m%dT%H%M%SZ")
        # The random part means a new file can never take the name of an existing one.
        path = f"{folder}/{unit}{SEPARATOR}{stamp}{SEPARATOR}{uuid.uuid4().hex[:8]}.parquet"

        self.fs.create_dir(folder, recursive=True)
        # Write under a temporary name first: only complete files get the .parquet ending.
        with self.fs.open_output_stream(path + ".tmp") as out:
            pq.write_table(table, out)
        self.fs.move(path + ".tmp", path)
        return path

    def _files(self, kind, from_date=None):
        """(path, partition date) of every Parquet file of a kind, sorted by path."""
        selector = pafs.FileSelector(
            f"{self.root}/raw/{kind}", recursive=True, allow_not_found=True
        )
        found = []
        for info in self.fs.get_file_info(selector):
            if info.type != pafs.FileType.File or not info.path.endswith(".parquet"):
                continue
            partition = info.path.rsplit("/", 2)[-2]
            partition_date = date.fromisoformat(partition.split("=", 1)[1])
            if from_date is None or partition_date >= from_date:
                found.append((info.path, partition_date))
        return sorted(found)

    def _read(self, kind, from_date=None):
        paths = [path for path, _ in self._files(kind, from_date)]
        if not paths:
            return SCHEMAS[kind].empty_table().to_pandas()
        return ds.dataset(paths, schema=SCHEMAS[kind], filesystem=self.fs).to_table().to_pandas()
