"""Read the feature table and build the training and validation sets.

Only train and validation rows are built here. The test segment is scored once, at the end of
MS3, so the loader does not even read it: rows from the end of the validation segment on are
cut off right after reading.
"""

from dataclasses import dataclass

import pandas as pd

from flowguard.common.features import TARGET, feature_columns
from flowguard.common.raw_store import RawStore
from flowguard.common.split import TRAIN, VAL, assign_segments
from flowguard.common.stations import targets
from flowguard.common.weather_features import weather_columns
from flowguard.features.feature_store import FeatureStore

LEVEL = "level"
WEATHER = "weather"
FEATURE_SETS = (LEVEL, WEATHER)


def feature_names(stations, config, feature_set):
    """Model inputs of a feature set, without metadata and without the target.

    level: the BAFU features and the calendar. weather: the same plus the weather columns.
    """
    if feature_set not in FEATURE_SETS:
        raise ValueError(f"Unknown feature set {feature_set!r}")
    names = feature_columns(stations)
    if feature_set == WEATHER:
        names = names + weather_columns(config.weather_points)
    return names


def read_feature_table(store, config, stations):
    """Read all stored months of the target stations, up to the end of the validation segment.

    store is a local folder or a gs:// path. Returns (table, manifest). Rows with t at or after
    val_end are dropped before anything else sees them.
    """
    raw = RawStore(store)  # only used for the file system and the root path
    features = FeatureStore(raw.fs, raw.root, config.feature_version)
    manifest = features.read_manifest()
    if manifest is None:
        raise FileNotFoundError(f"No feature manifest under {features.base}")

    until = pd.Timestamp(config.split.val_end)
    parts = []
    for station in targets(stations):
        for month in features.months(station.number):
            # A month that starts at or after val_end holds no validation rows.
            if pd.Timestamp(f"{month}-01", tz="UTC") >= until:
                continue
            frame = features.read_month(station.number, month)
            parts.append(frame[frame["t"] < until])
    if not parts:
        raise FileNotFoundError(f"No feature files under {features.base}")
    return pd.concat(parts, ignore_index=True), manifest


@dataclass(frozen=True)
class Dataset:
    X: pd.DataFrame  # model inputs; station_id is the last column, a category
    y: pd.Series  # target: change in W over 24 h, in cm
    station_id: pd.Series
    t: pd.Series


@dataclass(frozen=True)
class Datasets:
    train: Dataset
    val: Dataset


def build_datasets(table, config, stations, feature_set, weather_era_only=False):
    """Train and validation sets for one feature set.

    A row belongs to a segment only if t and t + 24 h are inside it (assign_segments), and
    only rows with a target are kept. The weather set needs weather, so its training rows start
    at the first weather origin. weather_era_only gives the level set the same training rows,
    for a fair comparison with and without weather. Validation rows are the same in all cases.
    """
    names = feature_names(stations, config, feature_set)
    segments = assign_segments(pd.DatetimeIndex(table["t"].unique()), config.split)
    segment = table["t"].map(segments)
    has_target = table[TARGET].notna()

    train_mask = (segment == TRAIN) & has_target
    if feature_set == WEATHER or weather_era_only:
        train_mask &= table["t"] >= config.first_weather_origin
    val_mask = (segment == VAL) & has_target

    categories = [s.number for s in targets(stations)]
    return Datasets(
        train=_dataset(table[train_mask], names, categories),
        val=_dataset(table[val_mask], names, categories),
    )


def _dataset(rows, names, categories):
    rows = rows.sort_values(["station_id", "t"], kind="stable")
    X = rows[names].copy()
    # A fixed category list gives the same codes in training and validation.
    X["station_id"] = pd.Categorical(rows["station_id"], categories=categories)
    return Dataset(
        X=X.reset_index(drop=True),
        y=rows[TARGET].reset_index(drop=True),
        station_id=rows["station_id"].reset_index(drop=True),
        t=rows["t"].reset_index(drop=True),
    )
