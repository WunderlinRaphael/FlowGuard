"""Weather features per origin hour t, built from the stored Open-Meteo model runs.

For an origin t the run rule (select_run) picks the run S that was available at t + 3 h.
The weather series seen at t is a composite of runs, built the same way in training and live:
- valid times up to S + 6 h come from leads 1 to 6 of the run that started just before them,
- valid times after S + 6 h come from run S itself.
Only runs up to S are used, so a row at t never depends on a run that came later.

A value at valid time v covers the hour that ends at v (BAFU stamps mark the start of the
hour). The origin hour t is complete at c = t + 1 h, so "observed-like" windows end at valid
time c and "forecast" windows start after it.

Missing runs (29.10.2026): a missing selected run is replaced by the newest stored run before
it. A run covers an hour for a variable and a point only if its value there is not null. If it
does not (run missing, a variable all null, a single null hour), the same valid time is read
from the newest older stored run that has a value (lead 7 to 12, ...), also after S + 6 h. An
hour no run covers is missing and so is every window that holds it (strict windows). Origins before first_weather_origin keep their row with missing weather columns.

Everything is computed per group of origins that share the offset d = t - S, as numpy arrays
over all runs at once. There is no loop over single origins or points.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from flowguard.features.weather_client import VARIABLES

RUN_CYCLE = pd.Timedelta(hours=6)
SHORT_LEADS = 6  # the composite takes leads 1 to 6 of every run up to S
MAX_LEAD = 47  # a stored run has leads 1 to 47 (48 hours, hour 0 is dropped by the client)
MAX_FALLBACK = 8  # a valid time is looked up in at most 8 older runs (lead + 6 * j <= 47)
RUN_AGE_COLUMN = "wx_run_age_h"

# Window of the composite series seen from an origin: 72 h observed-like (ending at c) and
# 24 h forecast. rel = valid time - S in hours, so the window holds rel = d - 70 .. d + 25.
OBSERVED_HOURS = 72
FORECAST_HOURS = 24
WINDOW_LENGTH = OBSERVED_HOURS + FORECAST_HOURS
# Index of the first forecast hour (valid time c + 1) inside the window.
FORECAST_START = OBSERVED_HOURS

PRECIPITATION = VARIABLES.index("precipitation")
TEMPERATURE = VARIABLES.index("temperature_2m")
SNOWFALL = VARIABLES.index("snowfall")

# (column suffix, variable, first index, last index + 1, reducer). Observed windows end at c.
FEATURES = [
    ("prec_obs_24h", PRECIPITATION, 48, 72, np.sum),
    ("prec_obs_48h", PRECIPITATION, 24, 72, np.sum),
    ("prec_obs_72h", PRECIPITATION, 0, 72, np.sum),
    ("tmean_obs_24h", TEMPERATURE, 48, 72, np.mean),
    ("tmax_obs_24h", TEMPERATURE, 48, 72, np.max),
    ("prec_fc_next6h", PRECIPITATION, 72, 78, np.sum),
    ("prec_fc_next12h", PRECIPITATION, 72, 84, np.sum),
    ("prec_fc_next24h", PRECIPITATION, 72, 96, np.sum),
    ("prec_fc_peak24h", PRECIPITATION, 72, 96, np.max),
    ("tmean_fc_24h", TEMPERATURE, 72, 96, np.mean),
    ("tmax_fc_24h", TEMPERATURE, 72, 96, np.max),
    ("snow_fc_next24h", SNOWFALL, 72, 96, np.sum),
]


@dataclass(frozen=True)
class RunCube:
    """All stored runs as one array.

    values[variable, point, run, lead - 1]; runs lie every 6 h from first_run, a run that is
    not stored is all NaN and stored[run] is False. first_run is None for an empty store.
    """

    first_run: pd.Timestamp | None
    stored: np.ndarray
    values: np.ndarray


def weather_columns(points):
    """Weather feature names in table order (the run age is last). points: names or WeatherPoints."""
    names = [getattr(p, "name", p) for p in points]
    return [f"wx_{name}_{suffix}" for name in names for suffix, *_ in FEATURES] + [RUN_AGE_COLUMN]


def load_runs(weather_rows, points):
    """Build the RunCube from raw weather rows (a DataFrame or a list of dicts).

    If a value was stored more than once, the version with the newest fetched_at wins (the
    same rule as the hourly grid). Rows without run_start (observed rows) are not used.
    """
    names = [getattr(p, "name", p) for p in points]
    df = weather_rows if isinstance(weather_rows, pd.DataFrame) else pd.DataFrame(weather_rows)
    n_points = len(names)
    if df.empty or df["run_start"].isna().all():
        return RunCube(
            None, np.zeros(0, dtype=bool), np.full((len(VARIABLES), n_points, 0, MAX_LEAD), np.nan)
        )

    df = df[df["run_start"].notna() & df["point"].isin(names) & df["parameter"].isin(VARIABLES)]
    df = df.assign(
        run_start=pd.to_datetime(df["run_start"], utc=True),
        valid_time=pd.to_datetime(df["valid_time"], utc=True),
    )
    starts = df["run_start"].drop_duplicates()
    if ((starts.dt.hour % 6 != 0) | (starts.dt.minute != 0) | (starts.dt.second != 0)).any():
        raise ValueError("Run starts must lie on the 6 h cycle (00, 06, 12, 18 UTC)")

    key = ["point", "valid_time", "run_start", "parameter"]
    df = df.sort_values("fetched_at", kind="stable").drop_duplicates(key, keep="last")
    lead = ((df["valid_time"] - df["run_start"]) // pd.Timedelta(hours=1)).to_numpy()
    keep = (lead >= 1) & (lead <= MAX_LEAD)
    df, lead = df[keep], lead[keep]

    first = df["run_start"].min()
    run = ((df["run_start"] - first) // RUN_CYCLE).to_numpy()
    n_runs = int(run.max()) + 1
    variable = pd.Index(VARIABLES).get_indexer(df["parameter"])
    point = pd.Index(names).get_indexer(df["point"])

    values = np.full((len(VARIABLES), n_points, n_runs, MAX_LEAD), np.nan)
    values[variable, point, run, lead - 1] = df["value"].to_numpy(dtype=float)
    stored = np.zeros(n_runs, dtype=bool)
    stored[run] = True
    return RunCube(first, stored, values)


def select_run_starts(origins, *, as_of_offset, min_run_age):
    """Start of the run chosen for every origin: the vectorised form of weather_client.select_run.

    A test checks that both give the same answer for every hour.
    """
    return (origins + as_of_offset - min_run_age).floor(RUN_CYCLE)


def resolve_stored_run(selected, stored):
    """Newest stored run at or before each selected run index (-1 if there is none)."""
    stored_idx = np.flatnonzero(stored)
    if len(stored_idx) == 0:
        return np.full(len(selected), -1)
    pos = np.searchsorted(stored_idx, selected, side="right") - 1
    return np.where(pos >= 0, stored_idx[np.maximum(pos, 0)], -1)


def composite_windows(cube, run_idx, d):
    """Composite series for the runs run_idx, seen from origins d hours after the run start.

    Returns an array (variable, point, run, 96) over the valid times S + d - 70 .. S + d + 25.
    Index 71 is c (the hour of the origin), index 72 the first forecast hour.
    """
    run_idx = np.asarray(run_idx)
    n_vars, n_points, n_runs, _ = cube.values.shape
    out = np.full((n_vars, n_points, len(run_idx), WINDOW_LENGTH), np.nan)
    # rel = d - 70 .. d + 25: 72 hours up to c = t + 1 h (rel d + 1) and 24 hours after it.
    rel = np.arange(d + 1 - (OBSERVED_HOURS - 1), d + 1 + FORECAST_HOURS + 1)

    # Where each valid time is read first. Up to S + 6 h: leads 1 to 6 of the run that started
    # just before it (cycle 0 for rel 1..6, -1 for rel -5..0, ...). After S + 6 h: run S itself
    # at lead rel (cycle 0).
    early = rel <= SHORT_LEADS
    cycle = np.where(early, (rel - 1) // SHORT_LEADS, 0)
    lead0 = np.where(early, rel - SHORT_LEADS * cycle, rel)

    # A run covers a valid time for a variable and a point only if its value there is not
    # null. If not, the same valid time is read from the next older run (lead + 6 h each time).
    # A run that is not stored is all NaN, so it is skipped the same way.
    filled = np.zeros(out.shape, dtype=bool)
    for j in range(MAX_FALLBACK):
        r = run_idx[:, None] + cycle[None, :] - j
        lead = lead0 + SHORT_LEADS * j
        in_range = (r >= 0) & (r < n_runs) & (lead <= MAX_LEAD)[None, :]
        r_safe = np.clip(r, 0, n_runs - 1)
        found = cube.values[:, :, r_safe, np.clip(lead, 1, MAX_LEAD)[None, :] - 1]
        usable = in_range[None, None] & ~np.isnan(found) & ~filled
        out = np.where(usable, found, out)
        filled |= usable
    return out


def build_weather_features(weather_rows, origins, config):
    """Weather features for the origin hours (UTC, on the hour), one row per origin.

    Returns a DataFrame with t, the columns of weather_columns and wx_run_age_h, in the order
    of the origins. It needs only runs up to the selected run of each origin, so live can pass
    just the newest origins. Weather does not depend on the station: the result is joined to
    the station rows on t.
    """
    origins = _check_origins(origins)
    points = config.weather_points
    columns = weather_columns(points)
    n_features = len(FEATURES)
    result = np.full((len(origins), len(points), n_features), np.nan)
    age = np.full(len(origins), np.nan)

    cube = load_runs(weather_rows, points)
    if len(origins) and cube.first_run is not None:
        selected = select_run_starts(
            origins, as_of_offset=config.as_of_offset, min_run_age=config.min_run_age
        )
        selected_idx = ((selected - cube.first_run) // RUN_CYCLE).to_numpy()
        used_idx = resolve_stored_run(selected_idx, cube.stored)
        used_start = cube.first_run + pd.to_timedelta(used_idx * 6, unit="h")
        offset = ((origins - used_start) // pd.Timedelta(hours=1)).to_numpy()
        usable = (used_idx >= 0) & (origins >= config.first_weather_origin)

        # One pass per distinct offset d (normally the six values 4 to 9), all runs at once.
        for d in np.unique(offset[usable]):
            rows = np.flatnonzero(usable & (offset == d))
            window = composite_windows(cube, used_idx[rows], int(d))
            for f, (_, variable, start, stop, reduce) in enumerate(FEATURES):
                # NaN in the window makes the result NaN (strict, no 75 percent rule).
                result[rows, :, f] = reduce(window[variable, :, :, start:stop], axis=-1).T
            age[rows] = d

    frame = pd.DataFrame(
        result.reshape(len(origins), len(points) * n_features), columns=columns[:-1]
    )
    frame[RUN_AGE_COLUMN] = age
    frame.insert(0, "t", origins)
    return frame


def _check_origins(origins):
    index = pd.DatetimeIndex(origins)
    if len(index) == 0:
        return index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    if index.tz is None:
        raise ValueError("Origins must be timezone-aware (UTC)")
    index = index.tz_convert("UTC")
    if (index != index.floor("h")).any():
        raise ValueError("Origins must lie on the hour")
    return index
