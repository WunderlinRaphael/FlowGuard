"""Storage of the feature table (Parquet) and its manifest.

Layout under the store path (a local folder or a gs:// URI, the same as the raw store):
    features/<feature_version>/station=<number>/month=<YYYY-MM>/features.parquet
    features/<feature_version>/_manifest.json

The file name is fixed, so a rebuild replaces the same file. A month file is only written if
its content differs from what is stored, so the bucket does not collect needless versions.
"""

import io
import json

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from flowguard.common.feature_table import STRING_COLUMNS, feature_table_columns

TIMESTAMP = pa.timestamp("us", tz="UTC")
FILE_NAME = "features.parquet"
MANIFEST_NAME = "_manifest.json"


def feature_schema(stations, config):
    """Explicit Arrow schema: strings for the metadata, a timestamp for t, floats otherwise."""
    fields = []
    for name in feature_table_columns(stations, config):
        if name == "t":
            fields.append((name, TIMESTAMP))
        elif name in STRING_COLUMNS:
            fields.append((name, pa.string()))
        else:
            fields.append((name, pa.float64()))
    return pa.schema(fields)


def to_arrow(frame, schema):
    """Convert the table to the stored schema (no pandas metadata, so the bytes are stable)."""
    table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
    return table.replace_schema_metadata(None)


def to_parquet_bytes(table):
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    return buffer.getvalue()


class FeatureStore:
    def __init__(self, fs, root, version):
        # fs and root come from the RawStore, so local folders and gs:// work the same way.
        self.fs, self.root, self.version = fs, str(root), version

    @property
    def base(self):
        return f"{self.root}/features/{self.version}"

    def month_path(self, station, month):
        return f"{self.base}/station={station}/month={month}/{FILE_NAME}"

    def write_month(self, station, month, table):
        """Store one month of one station. Returns False if the stored file is identical."""
        data = to_parquet_bytes(table)
        path = self.month_path(station, month)
        if self._same_bytes(path, data):
            return False
        self._write(path, data)
        return True

    def read_month(self, station, month):
        with self.fs.open_input_file(self.month_path(station, month)) as f:
            return pq.read_table(f).to_pandas()

    def months(self, station):
        """Months stored for a station, sorted."""
        selector = pafs.FileSelector(
            f"{self.base}/station={station}", recursive=True, allow_not_found=True
        )
        found = []
        for info in self.fs.get_file_info(selector):
            if info.type == pafs.FileType.File and info.path.endswith(FILE_NAME):
                found.append(info.path.rsplit("/", 2)[-2].split("=", 1)[1])
        return sorted(found)

    def read_manifest(self):
        path = f"{self.base}/{MANIFEST_NAME}"
        if self.fs.get_file_info(path).type != pafs.FileType.File:
            return None
        with self.fs.open_input_stream(path) as f:
            return json.loads(f.read().decode("utf-8"))

    def write_manifest(self, manifest):
        text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        self._write(f"{self.base}/{MANIFEST_NAME}", text.encode("utf-8"))

    def _same_bytes(self, path, data):
        info = self.fs.get_file_info(path)
        # Compare the bytes, not the tables: Arrow treats NaN as different from NaN.
        if info.type != pafs.FileType.File or info.size != len(data):
            return False
        with self.fs.open_input_stream(path) as f:
            return f.read() == data

    def _write(self, path, data):
        self.fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
        # Write under a temporary name first: only complete files get the final name.
        with self.fs.open_output_stream(path + ".tmp") as out:
            out.write(data)
        self.fs.move(path + ".tmp", path)
