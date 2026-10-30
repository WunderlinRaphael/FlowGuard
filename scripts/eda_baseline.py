"""One-off analysis: persistence baseline, event counts per segment and travel time.

Reads the raw BAFU rows from the store, builds the hourly grid, applies the cleaning checks
and writes docs/eda_notes.md. No model is trained. The pipelines do not import this file.

Run with: uv run python scripts/eda_baseline.py --store gs://flowguard-hs26-data
"""

import argparse
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from flowguard.common.cleaning import clean_series, rules_for
from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.raw_store import RawStore, default_store_path
from flowguard.common.split import TEST, TRAIN, VAL, assign_segments, validate_split
from flowguard.common.stations import load_stations
from flowguard.features.backfill import latest_run, run_unit, weather_runs
from flowguard.features.hourly_grid import build_hourly_grid

HORIZON = timedelta(hours=24)
# Origins whose band rows are more than this apart belong to different events.
EPISODE_GAP = timedelta(hours=24)
BANDS = ("low", "normal", "flood")
SEGMENTS = (TRAIN, VAL, TEST)
# The success criterion: MAE of at most 3 cm. Also used for the share of "large" changes.
THRESHOLD_CM = 3.0
# Below this many rows a band in a segment is too thin to say anything.
MIN_BAND_ROWS = 100
MAX_LAG = 24
REPORT_LAGS = (0, 3, 6, 9, 12)
# Fewer overlapping hours than this give no meaningful correlation.
MIN_PAIRS = 100
# Upstream series against Seedorf (2056): (station, parameter).
TRAVEL_PAIRS = [
    ("2087", "W"),
    ("2491", "W"),
    ("2087", "Q"),
    ("2491", "Q"),
    ("2492", "Q"),
    ("2649", "Q"),
]
SEEDORF = "2056"
# How far (weeks) a cut date is moved when looking for a better one.
SHIFT_WEEKS = range(-26, 27)


# --- building blocks (small, pure functions, tested in tests/test_eda_baseline.py) ---


def hourly_diff(s, hours=1):
    """s(t) - s(t - hours), by timestamp."""
    return s - s.shift(freq=timedelta(hours=hours)).reindex(s.index)


def build_targets(w, segments):
    """One row per origin hour t: W(t), the 24 h change in cm and the segment of t.

    The change is W(t + 24 h) - W(t), looked up by timestamp. It is missing if either value
    is missing, and then the row has no target.
    """
    future = w.shift(freq=-HORIZON).reindex(w.index)
    return pd.DataFrame({"w": w, "y_delta_cm": (future - w) * 100, "segment": segments})


def band_thresholds(frame):
    """10th and 90th percentile of W(t) over the train segment only."""
    train_w = frame.loc[frame["segment"] == TRAIN, "w"].dropna()
    return train_w.quantile(0.10), train_w.quantile(0.90)


def add_band(frame, low, high):
    """Band from W(t), which is known at forecast time: low <= p10, flood >= p90."""
    band = pd.Series("normal", index=frame.index, dtype="object")
    band[frame["w"] <= low] = "low"
    band[frame["w"] >= high] = "flood"
    band[frame["w"].isna()] = None
    return frame.assign(band=band)


def with_target(frame):
    return frame[frame["y_delta_cm"].notna()]


def persistence_stats(delta_cm):
    """Rows, MAE in cm and share of rows with an absolute change above 3 cm.

    Persistence predicts a change of 0, so its error is the absolute change itself.
    """
    if len(delta_cm) == 0:
        return 0, np.nan, np.nan
    error = delta_cm.abs()
    return len(delta_cm), error.mean(), (error > THRESHOLD_CM).mean()


def count_episodes(times):
    """Number of separate events: hours more than 24 h after the previous one start a new one."""
    times = pd.DatetimeIndex(times).sort_values()
    if len(times) == 0:
        return 0
    return 1 + int((times[1:] - times[:-1] > EPISODE_GAP).sum())


def lag_correlations(upstream, downstream, max_lag=MAX_LAG):
    """Correlation of upstream(t - k) with downstream(t) for k = 0 .. max_lag, by timestamp."""
    result = {}
    for k in range(max_lag + 1):
        moved = upstream.shift(freq=timedelta(hours=k))
        pair = pd.concat([moved, downstream], axis=1, join="inner").dropna()
        result[k] = pair.iloc[:, 0].corr(pair.iloc[:, 1]) if len(pair) >= MIN_PAIRS else np.nan
    return pd.Series(result)


def only_train(s, segments):
    """Values inside the train segment, everything else missing."""
    return s.where(segments == TRAIN)


def empty_release(release_state):
    return release_state.isna() | (release_state == "")


# --- the analysis ---


@dataclass
class Analysis:
    volume: pd.DataFrame
    flags: pd.DataFrame
    thresholds: pd.DataFrame
    persistence: pd.DataFrame
    events: pd.DataFrame
    events_weather_era: pd.DataFrame
    release: pd.DataFrame
    suggestions: list
    travel: pd.DataFrame
    travel_6h: pd.DataFrame
    diversion: dict


def analyse(grid, cfg, stations):
    """All tables for the notes. stations: the entries of config/stations.yaml."""
    names = {s.number: s.name for s in stations}
    index = pd.date_range(grid["timestamp"].min(), grid["timestamp"].max(), freq="h", unit="ns")
    segments = assign_segments(index, cfg.split)

    raw, release, clean, flag_rows, volume_rows = {}, {}, {}, [], []
    for (station, parameter), d in grid.groupby(["station", "parameter"]):
        d = d.set_index("timestamp")
        volume_rows.append(
            {
                "station": f"{station} {names[station]}",
                "parameter": parameter,
                "first": d.index.min(),
                "last": d.index.max(),
                "hours": len(d),
                "missing_percent": d["is_missing"].mean() * 100,
            }
        )
        raw[station, parameter] = d["value"].reindex(index)
        release[station, parameter] = d["release_state"].reindex(index)
        if parameter in ("Q", "W"):
            result = clean_series(
                raw[station, parameter], rules_for(cfg.cleaning, station, parameter)
            )
            clean[station, parameter] = result.values
            counts = result.flags.value_counts()
            flag_rows.append(
                {
                    "station": f"{station} {names[station]}",
                    "parameter": parameter,
                    **{k: int(counts.get(k, 0)) for k in ("range", "spike", "flat")},
                }
            )

    targets = [s.number for s in stations if s.role == "target"]
    frames, threshold_rows = {}, []
    for station in targets:
        frame = build_targets(clean[station, "W"], segments)
        low, high = band_thresholds(frame)
        frame = add_band(frame, low, high)
        empty = empty_release(release[station, "W"])
        frame["empty_t"] = empty
        frame["empty_target"] = empty.shift(freq=-HORIZON).reindex(index)
        frames[station] = frame
        threshold_rows.append(
            {"station": f"{station} {names[station]}", "p10_w": low, "p90_w": high}
        )

    events = band_events(frames, names)
    weather_era = band_events(frames, names, from_origin=cfg.first_weather_origin)
    return Analysis(
        volume=pd.DataFrame(volume_rows),
        flags=pd.DataFrame(flag_rows),
        thresholds=pd.DataFrame(threshold_rows),
        persistence=persistence_table(frames, names),
        events=events,
        events_weather_era=weather_era[weather_era["segment"] == TRAIN],
        release=release_table(frames, names),
        suggestions=suggest_shifts(frames, events, cfg.split),
        travel=travel_table(clean, segments),
        travel_6h=travel_table(clean, segments, hours=6),
        diversion=diversion_stats(clean, segments),
    )


def persistence_table(frames, names):
    rows = []
    for station, frame in frames.items():
        for segment in SEGMENTS:
            part = with_target(frame[frame["segment"] == segment])
            for band in ("all", *BANDS):
                sub = part if band == "all" else part[part["band"] == band]
                n, mae, share = persistence_stats(sub["y_delta_cm"])
                rows.append(
                    {
                        "station": f"{station} {names[station]}",
                        "segment": segment,
                        "band": band,
                        "rows": n,
                        "mae_cm": mae,
                        "share_over_3cm": share,
                    }
                )
    return pd.DataFrame(rows)


def band_events(frames, names, from_origin=None):
    """Rows with a target per segment and band, and how many separate events they form."""
    rows = []
    for station, frame in frames.items():
        part = with_target(frame)
        if from_origin is not None:
            part = part[part.index >= from_origin]
        for segment in SEGMENTS:
            in_segment = part[part["segment"] == segment]
            for band in BANDS:
                times = in_segment.index[in_segment["band"] == band]
                rows.append(
                    {
                        "station": f"{station} {names[station]}",
                        "station_no": station,
                        "segment": segment,
                        "band": band,
                        "rows": len(times),
                        "episodes": count_episodes(times),
                    }
                )
    return pd.DataFrame(rows)


def release_table(frames, names):
    rows = []
    for station, frame in frames.items():
        part = with_target(frame)
        for segment in SEGMENTS:
            sub = part[part["segment"] == segment]
            rows.append(
                {
                    "station": f"{station} {names[station]}",
                    "segment": segment,
                    "rows": len(sub),
                    "empty_at_t": sub["empty_t"].mean() if len(sub) else np.nan,
                    "empty_at_t_plus_24h": sub["empty_target"].mean() if len(sub) else np.nan,
                }
            )
    return pd.DataFrame(rows)


def thin_bands(events):
    """Val and test bands with fewer than MIN_BAND_ROWS rows."""
    holdout = events[events["segment"].isin([VAL, TEST])]
    return holdout[holdout["rows"] < MIN_BAND_ROWS]


def suggest_shifts(frames, events, split):
    """If a val or test band is thin, look for a cut date with fewer thin bands.

    Moves one cut (both neighbouring dates together) by whole weeks and keeps the shift with
    the fewest thin bands, the smallest move if tied. The band thresholds stay the ones from
    the current train segment. Returns (cut, new date, weeks, thin before, thin after) per cut;
    new date is None if no move helps. Only a suggestion.
    """
    before = len(thin_bands(events))
    if before == 0:
        return []
    suggestions = []
    for cut, partner in (("train_end", "val_start"), ("val_end", "test_start")):
        best = (before, 0, None)
        for weeks in sorted(SHIFT_WEEKS, key=abs):
            moved = getattr(split, cut) + timedelta(weeks=weeks)
            candidate = replace(split, **{cut: moved, partner: moved})
            try:
                validate_split(candidate)
            except ValueError:
                continue
            thin = len(thin_bands(band_events(shifted(frames, candidate), {s: s for s in frames})))
            if thin < best[0]:
                best = (thin, weeks, moved)
        thin, weeks, moved = best
        suggestions.append((cut, moved, weeks, before, thin))
    return suggestions


def shifted(frames, split):
    out = {}
    for station, frame in frames.items():
        out[station] = frame.assign(segment=assign_segments(frame.index, split))
    return out


def travel_table(clean, segments, hours=1):
    """Best lag and correlation of changes over `hours`, upstream against Seedorf, train only."""
    rows = []
    for station, parameter in TRAVEL_PAIRS:
        if (station, parameter) not in clean or (SEEDORF, parameter) not in clean:
            continue
        up = only_train(hourly_diff(clean[station, parameter], hours), segments)
        down = only_train(hourly_diff(clean[SEEDORF, parameter], hours), segments)
        corr = lag_correlations(up, down)
        best = corr.dropna()
        row = {
            "pair": f"{station} -> {SEEDORF}",
            "parameter": parameter,
            "best_lag_h": int(best.idxmax()) if len(best) else None,
            "corr_at_best": best.max() if len(best) else np.nan,
        }
        row.update({f"lag_{k}h": corr[k] for k in REPORT_LAGS})
        rows.append(row)
    return pd.DataFrame(rows)


def diversion_stats(clean, segments):
    """Is Q at 2649 roughly Q at 2492 plus something? Train segment, hours with both values."""
    a = only_train(clean["2492", "Q"], segments)
    b = only_train(clean["2649", "Q"], segments)
    pair = pd.concat([a, b], axis=1, join="inner").dropna()
    pair.columns = ["q2492", "q2649"]
    if len(pair) < MIN_PAIRS:
        return {"hours": len(pair)}
    slope, intercept = np.polyfit(pair["q2492"], pair["q2649"], 1)
    diffs = pd.concat([hourly_diff(a), hourly_diff(b)], axis=1, join="inner").dropna()
    return {
        "hours": len(pair),
        "corr_levels": pair["q2492"].corr(pair["q2649"]),
        "corr_hourly_changes": diffs.iloc[:, 0].corr(diffs.iloc[:, 1]),
        "slope": slope,
        "intercept": intercept,
        "r2": pair["q2492"].corr(pair["q2649"]) ** 2,
        "share_2649_at_least_2492": (pair["q2649"] >= pair["q2492"]).mean(),
        "median_2649_minus_2492": (pair["q2649"] - pair["q2492"]).median(),
    }


def missing_weather_runs(store, cfg, now):
    """Run starts the backfill rule expects but the store does not hold."""
    expected = weather_runs(cfg.weather_start, latest_run(now, cfg))
    stored = store.units("weather")
    return [run for run in expected if run_unit(run) not in stored]


# --- text output ---


def fmt(value):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "n/a"
    if isinstance(value, float | np.floating):
        return f"{value:.2f}"
    if isinstance(value, pd.Timestamp):
        return f"{value:%Y-%m-%d %H:%M}"
    return str(value)


def md_table(frame):
    lines = ["| " + " | ".join(frame.columns) + " |", "|" + "---|" * len(frame.columns)]
    for row in frame.itertuples(index=False):
        lines.append("| " + " | ".join(fmt(v) for v in row) + " |")
    return "\n".join(lines)


def persistence_sentences(persistence):
    """One plain sentence per station and holdout segment: is 3 cm easy for persistence?

    Only bands with at least MIN_BAND_ROWS rows count; thinner ones say too little.
    """
    lines = []
    holdout = persistence[persistence["segment"].isin([VAL, TEST])]
    for (station, segment), part in holdout.groupby(["station", "segment"]):
        bands = part[(part["band"] != "all") & (part["rows"] >= MIN_BAND_ROWS)]
        easy = int((bands["mae_cm"] <= THRESHOLD_CM).sum())
        overall = part[part["band"] == "all"]["mae_cm"].iloc[0]
        if easy * 2 > len(bands):
            verdict = "Persistence is already at or below 3 cm in most bands: 3 cm is easy here."
        elif easy * 2 == len(bands):
            verdict = "Persistence is at or below 3 cm in half of the bands."
        else:
            verdict = "Persistence is above 3 cm in most bands: 3 cm is a real target here."
        lines.append(
            f"- {station}, {segment}: overall MAE {fmt(overall)} cm, {easy} of {len(bands)} "
            f"bands with at least {MIN_BAND_ROWS} rows at or below 3 cm. {verdict}"
        )
    return lines


def para(*parts):
    """One paragraph of the notes, written as several short strings."""
    return "".join(parts)


def render(a, snapshot, store, cfg, missing_runs, rows_read):
    split = cfg.split
    # The newest runs may simply not be fetched yet by the hourly ingest.
    recent = [r for r in missing_runs if r >= snapshot - timedelta(hours=12)]
    missing_runs = [r for r in missing_runs if r not in recent]
    out = [
        "# EDA notes: persistence baseline and travel time",
        "",
        para(
            f"Snapshot: {snapshot:%Y-%m-%d %H:%M} UTC, store `{store}`, {rows_read:,} raw rows. ",
            "Written by `scripts/eda_baseline.py`; rerun the script instead of editing this file.",
        ),
        "",
        para(
            "Rows are hourly origins t and neighbouring rows are strongly correlated, so a row ",
            "count overstates how much independent information a segment holds. The episode ",
            "counts are the better guide. All numbers use the cleaned series (checks from ",
            "`common/cleaning.py`).",
        ),
        "",
        "## Cut dates used (provisional, UTC)",
        "",
        para(
            f"train until {split.train_end:%Y-%m-%d}, validation {split.val_start:%Y-%m-%d} to ",
            f"{split.val_end:%Y-%m-%d}, test {split.test_start:%Y-%m-%d} to ",
            f"{split.test_end:%Y-%m-%d}, embargo {split.embargo_hours} h. ",
            f"First origin with weather features: {cfg.first_weather_origin:%Y-%m-%d %H:%M}.",
        ),
        "",
        "## Data volume (E3)",
        "",
        para(
            "Raw BAFU hourly means in the store. `missing_percent` counts the gaps between the ",
            "first and the last delivered hour of each series.",
        ),
        "",
        md_table(a.volume),
        "",
        "## Cleaning flags over the whole history",
        "",
        md_table(a.flags),
        "",
        "## Risk band thresholds",
        "",
        para(
            "10th and 90th percentile of W(t) in the train segment (m a.s.l.). A row is `low` ",
            "at or below p10, `flood` at or above p90, otherwise `normal`. The band comes from ",
            "W(t), which is known at forecast time.",
        ),
        "",
        md_table(a.thresholds),
        "",
        "## Persistence baseline",
        "",
        para(
            "MAE in cm of the rule W(t + 24 h) = W(t), only rows with a target and a segment ",
            "label. `share_over_3cm` is the share of rows with an absolute 24 h change above 3 cm.",
        ),
        "",
        md_table(a.persistence),
        "",
        *persistence_sentences(a.persistence),
        "",
        "## Events per segment",
        "",
        para(
            "Rows with a target per band, and `episodes`: separate events (band hours more than ",
            f"24 h apart count as different events). A band under {MIN_BAND_ROWS} rows is thin.",
        ),
        "",
        md_table(a.events.drop(columns="station_no")),
        "",
    ]
    thin = thin_bands(a.events)
    if thin.empty:
        out.append(f"Every validation and test band has at least {MIN_BAND_ROWS} rows.")
    else:
        out.append(f"Thin bands (under {MIN_BAND_ROWS} rows) in validation or test:")
        out.append("")
        for row in thin.itertuples():
            out.append(f"- {row.station}, {row.segment}, {row.band}: {row.rows} rows")
        out.append("")
        for cut, moved, weeks, before, after in a.suggestions:
            if moved is None:
                out.append(f"- Moving `{cut}` by up to 26 weeks does not reduce the thin bands.")
            else:
                out.append(
                    para(
                        f"- Best move of `{cut}` (and its neighbour): to {moved:%Y-%m-%d} ",
                        f"({weeks:+d} weeks), thin bands {before} -> {after}.",
                    )
                )
    out += [
        "",
        "## Train events, all rows and weather era",
        "",
        para(
            "Train segment only. The second table counts only origins from ",
            f"{cfg.first_weather_origin:%Y-%m-%d %H:%M}, the first origin of the model with ",
            "weather.",
        ),
        "",
        "All rows:",
        "",
        md_table(a.events[a.events["segment"] == TRAIN].drop(columns=["station_no", "segment"])),
        "",
        "From the first weather origin on:",
        "",
        md_table(a.events_weather_era.drop(columns=["station_no", "segment"])),
        "",
        "## Release state",
        "",
        para(
            "Share of rows (with a target) whose W value has an empty release state, for the ",
            "forecast hour t and for the target hour t + 24 h. Empty means not yet validated.",
        ),
        "",
        md_table(a.release),
        "",
        "## Travel time",
        "",
        para(
            "Correlation of the hourly change of the upstream series at t - k with the hourly ",
            f"change at Seedorf ({SEEDORF}) at t, train segment only, k = 0 to {MAX_LAG} h.",
        ),
        "",
        md_table(a.travel),
        "",
        "Same with changes over 6 h instead of 1 h (less noise from hydropeaking):",
        "",
        md_table(a.travel_6h),
        "",
        "## Diversion flows 2492 and 2649",
        "",
        para(
            "Hourly Q in the train segment, hours where both have a value. Fit: Q2649 = slope * ",
            "Q2492 + intercept.",
        ),
        "",
        *[f"- {key}: {fmt(value)}" for key, value in a.diversion.items()],
        "",
        "## Weather runs not in the store",
        "",
        para(
            f"{len(missing_runs)} expected runs are not stored: ",
            ", ".join(run_unit(r) for r in missing_runs) if missing_runs else "none",
            ". Open-Meteo does not have these runs (the backfill skipped them with a warning).",
        ),
        "",
        para(
            f"Not counted: {len(recent)} run(s) from the last 12 hours, which the hourly ",
            "ingest may not have fetched yet: ",
            ", ".join(run_unit(r) for r in recent) if recent else "none",
            ".",
        ),
        "",
    ]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", default=default_store_path())
    parser.add_argument("--out", default="docs/eda_notes.md")
    args = parser.parse_args()

    cfg = load_pipeline_config()
    store = RawStore(args.store)
    try:
        raw = store.read_bafu()
        missing_runs = missing_weather_runs(store, cfg, datetime.now(UTC))
    except OSError as error:
        sys.exit(f"Cannot read the store {args.store}: {error}")
    print(f"Read {len(raw):,} raw rows from {args.store}")

    grid = build_hourly_grid(raw.to_dict("records")).grid
    analysis = analyse(grid, cfg, load_stations())
    text = render(analysis, datetime.now(UTC), args.store, cfg, missing_runs, len(raw))
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"Wrote {args.out}; missing weather runs: {len(missing_runs)}")


if __name__ == "__main__":
    main()
