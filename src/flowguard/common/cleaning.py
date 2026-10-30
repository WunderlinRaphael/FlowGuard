"""Cleaning checks for one hourly series: range, spike and flat line.

The checks flag values, they never delete them from the raw store. A flagged value becomes
missing in the cleaned series (no interpolation). The function is pure, training and the live
system call the same one.
"""

from dataclasses import dataclass, fields

import numpy as np
import pandas as pd

OK = "ok"
RANGE = "range"
SPIKE = "spike"
FLAT = "flat"
# The input value was already missing (a gap), so there was nothing to check.
MISSING = "missing"

PARAMETERS = ("Q", "W")
# A spike returns to the old level within this many hours; a longer jump is a real change.
MAX_SPIKE_HOURS = 2


@dataclass(frozen=True)
class CleaningRules:
    min: float
    max: float
    max_step: float
    # None switches the flat-line check off (for example a diversion that is often constant).
    flat_hours: int | None


@dataclass(frozen=True)
class CleanResult:
    values: pd.Series
    flags: pd.Series


@dataclass(frozen=True)
class CleaningConfig:
    # parameter -> partial rules valid for every station
    defaults: dict
    # station number -> parameter -> partial rules that override the defaults
    stations: dict


def rules_for(config, station, parameter):
    """Default rules of the parameter, overridden field by field for the station."""
    merged = {
        **config.defaults.get(parameter, {}),
        **config.stations.get(station, {}).get(parameter, {}),
    }
    return build_rules(merged, f"station {station} {parameter}")


def build_rules(raw, where="rules"):
    names = [f.name for f in fields(CleaningRules)]
    unknown = set(raw) - set(names)
    if unknown:
        raise ValueError(f"Cleaning {where}: unknown keys {sorted(unknown)}")
    missing = [name for name in names if name not in raw]
    if missing:
        raise ValueError(f"Cleaning {where}: missing keys {missing}")
    rules = CleaningRules(**raw)
    if rules.min >= rules.max:
        raise ValueError(f"Cleaning {where}: min must be below max")
    if rules.max_step <= 0:
        raise ValueError(f"Cleaning {where}: max_step must be positive")
    if rules.flat_hours is not None and rules.flat_hours < 2:
        raise ValueError(f"Cleaning {where}: flat_hours must be at least 2 or null")
    return rules


def parse_cleaning_config(raw):
    """Build the config from the `cleaning` section and check every station rule is complete."""
    config = CleaningConfig(
        defaults=dict(raw.get("defaults") or {}),
        stations={str(k): dict(v) for k, v in (raw.get("stations") or {}).items()},
    )
    for parameter in config.defaults:
        if parameter not in PARAMETERS:
            raise ValueError(f"Cleaning defaults: unknown parameter {parameter!r}")
    for station, parameters in config.stations.items():
        for parameter in parameters:
            if parameter not in PARAMETERS:
                raise ValueError(f"Cleaning station {station}: unknown parameter {parameter!r}")
            rules_for(config, station, parameter)
    return config


def clean_series(series, rules):
    """Check one series on the regular hourly grid; returns CleanResult(values, flags).

    series: float Series with a UTC hourly DatetimeIndex (a missing hour is NaN).
    flags: "ok", "range", "spike", "flat" or "missing" per hour. Order of precedence:
    range, spike, flat. Flagged values are NaN in `values`.
    """
    check_hourly_index(series.index)
    x = series.astype("float64")
    valid = x.notna()

    out_of_range = valid & ((x < rules.min) | (x > rules.max))
    spike = _spikes(x, rules.max_step) & ~out_of_range
    flat = _flat_lines(x, rules.flat_hours) & ~out_of_range & ~spike

    flags = pd.Series(OK, index=x.index, dtype="object")
    flags[~valid] = MISSING
    # Later assignments are weaker checks, so write them first.
    flags[flat] = FLAT
    flags[spike] = SPIKE
    flags[out_of_range] = RANGE

    values = x.mask(out_of_range | spike | flat)
    return CleanResult(values, flags)


def check_hourly_index(index):
    """Raise ValueError unless the index is a strictly regular hourly UTC DatetimeIndex."""
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("Series needs a timezone-aware DatetimeIndex")
    if str(index.tz) != "UTC":
        raise ValueError(f"Series index must be UTC, got {index.tz}")
    if len(index) > 1 and not (index[1:] - index[:-1] == pd.Timedelta(hours=1)).all():
        raise ValueError("Series index must be a regular hourly grid without holes or duplicates")


def _spikes(x, max_step):
    """Runs of 1 or 2 valid hours far from the hour before that return to it afterwards."""
    values = x.to_numpy()
    n = len(values)
    spike = np.zeros(n, dtype=bool)
    for length in range(1, MAX_SPIKE_HOURS + 1):
        before = _shift(values, 1)
        after = _shift(values, -length)
        far = np.ones(n, dtype=bool)
        for k in range(length):
            far &= np.abs(_shift(values, -k) - before) > max_step
        # NaN comparisons are False, so a missing neighbour never makes a spike.
        returns = np.abs(after - before) <= max_step / 2
        start = far & returns
        for k in range(length):
            spike |= _shift(start, k, fill=False)
    return pd.Series(spike, index=x.index)


def _shift(a, by, fill=np.nan):
    """a moved so that out[i] = a[i - by]; positions without a source get `fill`."""
    out = np.full_like(a, fill)
    if by > 0:
        out[by:] = a[:-by]
    elif by < 0:
        out[:by] = a[-by:]
    else:
        out[:] = a
    return out


def _flat_lines(x, flat_hours):
    """Runs of at least flat_hours identical valid values. A missing hour ends a run."""
    if flat_hours is None:
        return pd.Series(False, index=x.index)
    # NaN != NaN, so every missing hour starts its own group and cuts the run around it.
    group = (x != x.shift()).cumsum()
    size = x.groupby(group).transform("size")
    return x.notna() & (size >= flat_hours)
