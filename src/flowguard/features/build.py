"""Build the feature table from the raw store and write it, with a manifest.

    uv run python -m flowguard.features.build --store <path or gs://...> [--since YYYY-MM-DD]

Without --since the whole raw store is read and every month is rebuilt. With --since D the
months from the month of D onward are rebuilt, and raw data is read from a lookback before
the start of that month. The content of the table depends only on the raw data (the clock
is used for built_at in the manifest, nothing else). A month file is only written if its
content changed.
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from flowguard.common.cleaning import FLAT, RANGE, SPIKE
from flowguard.common.feature_table import build_feature_table
from flowguard.common.features import TARGET, clean_grid
from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.raw_store import STORE_ENV, RawStore, default_store_path, now_utc
from flowguard.common.stations import load_stations
from flowguard.common.weather_features import weather_columns
from flowguard.features.feature_store import FeatureStore, feature_schema, to_arrow
from flowguard.features.hourly_grid import build_hourly_grid

log = logging.getLogger(__name__)

# Longest lag of any feature (w_d7d) and a safety day.
LONGEST_LAG = timedelta(days=7)
MARGIN = timedelta(days=1)
FLAG_NAMES = (RANGE, SPIKE, FLAT)


@dataclass(frozen=True)
class BuildResult:
    manifest: dict
    written: int
    unchanged: int
    report: str


def max_flat_hours(cleaning):
    """The longest flat-line window in the cleaning config (defaults and station overrides)."""
    rules = list(cleaning.defaults.values())
    rules += [r for station in cleaning.stations.values() for r in station.values()]
    return max((r["flat_hours"] for r in rules if r.get("flat_hours")), default=0)


def lookback(cleaning):
    """How far before the first rebuilt month raw data is read.

    A value at t depends on 7 days of lags, and each of those values on the flat-line check,
    which looks back flat_hours. With less history an incremental build could differ from a
    full build at the start of the month.
    """
    return LONGEST_LAG + timedelta(hours=max_flat_hours(cleaning)) + MARGIN


def month_start(day):
    return datetime(day.year, day.month, 1, tzinfo=UTC)


def build(raw, config, stations, since=None, clock=now_utc, code_version=None):
    """Build and store the feature table. since: a date, or None for a full build."""
    store = FeatureStore(raw.fs, raw.root, config.feature_version)
    first_month = month_start(since) if since else None

    if first_month is None:
        bafu, weather = raw.read_bafu(), raw.read_weather()
    else:
        start = first_month - lookback(config.cleaning)
        bafu = raw.read_bafu(from_timestamp=start)
        weather = raw.read_weather(from_run_date=start.date())
    fetched = {"bafu": _newest(bafu), "weather": _newest(weather)}

    grid = build_hourly_grid(bafu.to_dict("records")).grid
    table = build_feature_table(grid, weather, stations, config)
    if first_month is not None:
        table = table[table["t"] >= first_month]
    flags = _count_flags(grid, config, first_month)

    schema = feature_schema(stations, config)
    months = _month_stats(table, weather_columns(config.weather_points))
    written = unchanged = 0
    for (station, month), part in _by_station_and_month(table):
        if store.write_month(station, month, to_arrow(part.reset_index(drop=True), schema)):
            written += 1
        else:
            unchanged += 1

    old = store.read_manifest() if first_month is not None else None
    manifest = _manifest(config, clock, code_version, fetched, months, flags, old, first_month)
    store.write_manifest(manifest)
    return BuildResult(
        manifest, written, unchanged, _report(manifest, written, unchanged, old, since)
    )


def _newest(df):
    if df.empty:
        return None
    return df["fetched_at"].max().strftime("%Y-%m-%dT%H:%M:%SZ")


def _by_station_and_month(table):
    if table.empty:
        return []
    month = table["t"].dt.strftime("%Y-%m")
    return table.groupby([table["station_id"], month], sort=True)


def _month_stats(table, wx_columns):
    """rows, rows with a target, rows with all weather columns, first and last t per month."""
    stats = {}
    complete = table[wx_columns].notna().all(axis=1)
    for (station, month), part in _by_station_and_month(table):
        stats.setdefault(station, {})[month] = {
            "rows": len(part),
            "rows_with_target": int(part[TARGET].notna().sum()),
            "rows_with_weather": int(complete[part.index].sum()),
            "first_t": _iso(part["t"].min()),
            "last_t": _iso(part["t"].max()),
        }
    return stats


def _count_flags(grid, config, first_month):
    """Cleaning flags per series and month, from the full (non-causal) check."""
    if grid.empty:
        return {}
    _, flags = clean_grid(grid, config.cleaning)
    counts = {}
    for (station, parameter), series in flags.items():
        if first_month is not None:
            series = series[series.index >= first_month]
        series = series[series.isin(FLAG_NAMES)]
        if series.empty:
            continue
        months = series.index.strftime("%Y-%m")
        per_month = pd.crosstab(months, series.to_numpy())
        counts[f"{station}/{parameter}"] = {
            month: {name: int(row.get(name, 0)) for name in FLAG_NAMES}
            for month, row in per_month.iterrows()
        }
    return counts


def _manifest(config, clock, code_version, fetched, months, flags, old, first_month):
    first_label = first_month.strftime("%Y-%m") if first_month is not None else None
    if old is not None:
        # Keep the months before the rebuilt ones; the rebuilt ones come from this run.
        months = _merge(old["months"], months, first_label)
        flags = _merge(old["flag_months"], flags, first_label)
        fetched = {
            key: max(
                (v for v in (fetched[key], old["newest_fetched_at"].get(key)) if v), default=None
            )
            for key in fetched
        }
    return {
        "feature_version": config.feature_version,
        "built_at": clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "code_version": code_version or os.environ.get("GITHUB_SHA") or "local",
        "newest_fetched_at": fetched,
        "stations": {n: _station_total(m) for n, m in sorted(months.items())},
        "cleaning_flags": {
            series: {name: sum(m[name] for m in per_month.values()) for name in FLAG_NAMES}
            for series, per_month in sorted(flags.items())
        },
        "months": months,
        "flag_months": flags,
    }


def _merge(old, new, first_label):
    merged = {
        key: {m: v for m, v in per_month.items() if m < first_label}
        for key, per_month in old.items()
    }
    for key, per_month in new.items():
        merged.setdefault(key, {}).update(per_month)
    return {key: per_month for key, per_month in merged.items() if per_month}


def _station_total(per_month):
    values = list(per_month.values())
    return {
        "rows": sum(v["rows"] for v in values),
        "rows_with_target": sum(v["rows_with_target"] for v in values),
        "rows_with_weather": sum(v["rows_with_weather"] for v in values),
        "first_t": min(v["first_t"] for v in values),
        "last_t": max(v["last_t"] for v in values),
    }


def _iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _report(manifest, written, unchanged, old, since):
    mode = f"since {since}" if since else "full build"
    lines = [
        f"Feature build {manifest['feature_version']} ({mode}), code {manifest['code_version']}"
    ]
    if since and old is None:
        lines.append("  note: no earlier manifest found, totals cover the rebuilt months only")
    for number, s in manifest["stations"].items():
        lines.append(
            f"  {number}: {s['rows']} rows, {s['rows_with_target']} with target, "
            f"{s['rows_with_weather']} with weather, {s['first_t']} to {s['last_t']}"
        )
    for series, counts in manifest["cleaning_flags"].items():
        lines.append(f"  flags {series}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if not manifest["stations"]:
        lines.append("  no rows: the raw store holds no usable data")
    lines.append(f"  month files: {written} written, {unchanged} unchanged")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the feature table from the raw store.")
    parser.add_argument(
        "--store",
        default=default_store_path(),
        help=f"local folder or gs:// URI (default: ${STORE_ENV}, else data)",
    )
    parser.add_argument(
        "--since",
        type=date.fromisoformat,
        help="rebuild the months from the month of this date (YYYY-MM-DD); default: everything",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = build(RawStore(args.store), load_pipeline_config(), load_stations(), args.since)
    print(result.report)
    sys.exit(0 if result.manifest["stations"] else 1)


if __name__ == "__main__":
    main()
