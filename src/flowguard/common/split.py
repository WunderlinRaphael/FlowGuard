"""Chronological split of origin hours into train, validation and test, with an embargo.

The split is not stored in the feature table. Segments are assigned at training time from the
cut dates in config/pipeline.yaml, the same for all stations, never shuffled.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

TRAIN = "train"
VAL = "val"
TEST = "test"
# t lies in a segment, but its target time t + 24 h is outside: not usable for any segment.
EMBARGO = "embargo"
# After the end of the test segment: never used for training or selection.
LIVE = "live"

SEGMENTS = (TRAIN, VAL, TEST)


@dataclass(frozen=True)
class SplitConfig:
    # None: the train segment starts with the first available origin.
    train_start: datetime | None
    train_end: datetime
    val_start: datetime
    val_end: datetime
    test_start: datetime
    test_end: datetime
    embargo_hours: int = 24

    @property
    def embargo(self):
        return timedelta(hours=self.embargo_hours)

    def spans(self):
        """(segment, start, end) in time order; the span is [start, end), start may be None."""
        return [
            (TRAIN, self.train_start, self.train_end),
            (VAL, self.val_start, self.val_end),
            (TEST, self.test_start, self.test_end),
        ]


def parse_split_config(raw):
    """Build a SplitConfig from the `split` section of pipeline.yaml and validate it."""
    config = SplitConfig(
        train_start=_to_utc(raw.get("train_start"), "train_start"),
        train_end=_to_utc(raw["train_end"], "train_end"),
        val_start=_to_utc(raw["val_start"], "val_start"),
        val_end=_to_utc(raw["val_end"], "val_end"),
        test_start=_to_utc(raw["test_start"], "test_start"),
        test_end=_to_utc(raw["test_end"], "test_end"),
        embargo_hours=int(raw.get("embargo_hours", 24)),
    )
    validate_split(config)
    return config


def validate_split(config):
    """Raise ValueError if the segments overlap, are out of order or are too short."""
    if config.embargo_hours < 0:
        raise ValueError("embargo_hours must not be negative")
    # The embargo sits inside each segment, so neighbours may touch (train_end == val_start).
    if config.train_start is not None and config.train_start >= config.train_end:
        raise ValueError("train_start must be before train_end")
    if config.train_end > config.val_start:
        raise ValueError("Segments overlap or are out of order: train_end is after val_start")
    if config.val_end > config.test_start:
        raise ValueError("Segments overlap or are out of order: val_end is after test_start")
    for name, start, end in config.spans()[1:]:
        if end - start <= config.embargo:
            raise ValueError(f"Segment {name} is not longer than the embargo")


def assign_segments(origins, config):
    """Label every origin hour: train, val, test, embargo or live.

    An origin belongs to a segment only if t and t + embargo are both inside it, so the last
    embargo_hours of each segment are labelled embargo. From the end of the test segment on,
    origins are live. Works on timestamps, so the order of `origins` does not matter.
    """
    index = pd.DatetimeIndex(origins)
    if index.tz is None:
        raise ValueError("Origins must be timezone-aware (UTC)")
    labels = pd.Series(EMBARGO, index=index, dtype="object")
    target = index + config.embargo
    for name, start, end in config.spans():
        inside = (index < end) & (target < end)
        if start is not None:
            inside &= index >= start
        labels[inside] = name
    labels[index >= config.test_end] = LIVE
    return labels


def _to_utc(value, name):
    if value is None:
        return None
    ts = pd.Timestamp(value)
    # YAML has no time zone, so a date or datetime in the config means UTC.
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    if str(ts.tz) != "UTC":
        raise ValueError(f"Split {name} must be UTC")
    return ts.to_pydatetime()
