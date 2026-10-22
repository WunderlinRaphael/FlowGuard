"""Shared test helpers for the grid and gap report tests."""

from datetime import UTC, datetime, timedelta

START = datetime(2026, 10, 8, 0, tzinfo=UTC)
FETCHED = datetime(2026, 10, 9, 12, tzinfo=UTC)


def make_rows(hours, station="2087", parameter="W", start=START, fetched_at=FETCHED, **extra):
    """One row per hour offset in `hours`; the value is the offset itself."""
    return [
        {
            "station": station,
            "timestamp": start + timedelta(hours=h),
            "parameter": parameter,
            "value": float(h),
            "unit": "m ü.M.",
            "release_state": None,
            "fetched_at": fetched_at,
            **extra,
        }
        for h in hours
    ]
