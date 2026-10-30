from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from flowguard.common.split import assign_segments, parse_split_config

RAW = {
    "train_start": None,
    "train_end": "2026-03-15",
    "val_start": "2026-03-15",
    "val_end": "2026-06-15",
    "test_start": "2026-06-15",
    "test_end": "2026-10-01",
    "embargo_hours": 24,
}


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def config(**changes):
    return parse_split_config({**RAW, **changes})


def hours(start, end):
    return pd.date_range(start, end, freq="h", tz="UTC", inclusive="left")


def test_dates_are_read_as_utc():
    c = config()
    assert c.train_start is None
    assert c.val_start == utc(2026, 3, 15)
    assert c.test_end == utc(2026, 10, 1)


def test_each_segment_gets_its_rows():
    labels = assign_segments(hours("2026-01-01", "2026-11-01"), config())
    assert set(labels) == {"train", "val", "test", "embargo", "live"}
    assert labels[pd.Timestamp("2026-01-10", tz="UTC")] == "train"
    assert labels[pd.Timestamp("2026-04-10", tz="UTC")] == "val"
    assert labels[pd.Timestamp("2026-07-10", tz="UTC")] == "test"
    assert labels[pd.Timestamp("2026-10-15", tz="UTC")] == "live"


def test_last_24_hours_of_each_segment_are_embargo():
    c = config()
    labels = assign_segments(hours("2026-01-01", "2026-11-01"), c)
    for name, end in [("train", c.train_end), ("val", c.val_end), ("test", c.test_end)]:
        last_ok = pd.Timestamp(end) - timedelta(hours=25)
        assert labels[last_ok] == name
        for k in range(1, 25):
            assert labels[pd.Timestamp(end) - timedelta(hours=k)] == "embargo"


def test_first_hours_of_a_segment_belong_to_it():
    labels = assign_segments(hours("2026-03-14", "2026-03-17"), config())
    assert labels[pd.Timestamp("2026-03-15 00:00", tz="UTC")] == "val"
    assert labels[pd.Timestamp("2026-03-14 23:00", tz="UTC")] == "embargo"


def test_rows_from_test_end_on_are_live():
    labels = assign_segments(hours("2026-09-30", "2026-10-03"), config())
    assert labels[pd.Timestamp("2026-10-01 00:00", tz="UTC")] == "live"
    assert labels[pd.Timestamp("2026-09-30 23:00", tz="UTC")] == "embargo"
    assert (labels[labels.index >= pd.Timestamp("2026-10-01", tz="UTC")] == "live").all()


def test_target_time_of_every_row_stays_inside_its_segment():
    c = config()
    index = hours("2025-12-01", "2026-11-01")
    labels = assign_segments(index, c)
    target = labels.index + timedelta(hours=24)
    assert (target[labels == "train"] < c.val_start).all()
    assert (target[labels == "val"] < c.test_start).all()
    assert (target[labels == "test"] < c.test_end).all()


def test_labels_do_not_depend_on_row_order():
    index = hours("2026-03-10", "2026-03-20")
    shuffled = index.to_series().sample(frac=1, random_state=3).index
    a = assign_segments(index, config())
    b = assign_segments(shuffled, config()).sort_index()
    pd.testing.assert_series_equal(a, b, check_freq=False)


def test_train_start_cuts_off_earlier_origins():
    labels = assign_segments(hours("2026-01-01", "2026-01-05"), config(train_start="2026-01-03"))
    assert labels[pd.Timestamp("2026-01-02 12:00", tz="UTC")] == "embargo"
    assert labels[pd.Timestamp("2026-01-03 00:00", tz="UTC")] == "train"


def test_open_train_start_takes_old_origins():
    labels = assign_segments(pd.DatetimeIndex([pd.Timestamp("2011-01-01", tz="UTC")]), config())
    assert labels.iloc[0] == "train"


def test_gap_between_segments_is_embargo():
    c = config(val_start="2026-03-20")
    labels = assign_segments(hours("2026-03-15", "2026-03-20"), c)
    assert (labels == "embargo").all()


def test_naive_origins_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        assign_segments(pd.date_range("2026-01-01", periods=3, freq="h"), config())


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"val_start": "2026-03-01"}, "train_end is after val_start"),
        ({"test_start": "2026-06-01"}, "val_end is after test_start"),
        ({"val_end": "2026-03-15"}, "not longer than the embargo"),
        ({"test_end": "2026-06-15 12:00"}, "not longer than the embargo"),
        ({"train_start": "2026-04-01"}, "train_start must be before train_end"),
        ({"embargo_hours": -1}, "must not be negative"),
        ({"val_start": "2026-03-15 00:00+02:00"}, "must be UTC"),
    ],
)
def test_invalid_config_raises(changes, message):
    with pytest.raises(ValueError, match=message):
        config(**changes)
