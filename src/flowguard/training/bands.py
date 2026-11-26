"""Risk bands and episode counts, and which bands are evaluable (rules in docs/evaluation.md).

A band is only scored against persistence if it has enough separate events (episodes) in the
segment. The minimum, the episode gap and the pooling switch come from the `evaluation` section
of config/pipeline.yaml. No model code here: this module only counts.
"""

import pandas as pd

from flowguard.common.split import SEGMENTS, TRAIN

BANDS = ("low", "normal", "flood")
# The episode minimum and the pooling apply only to these bands. The normal band is the bulk of
# the rows and has few, very long episodes, so it is always evaluable when it has rows.
RARE_BANDS = ("low", "flood")
REPORT_COLUMNS = [
    "station",
    "segment",
    "band",
    "rows",
    "episodes",
    "pooled_episodes",
    "evaluable",
    "pooled",
]


def band_thresholds(w, segments):
    """10th and 90th percentile of W(t) over the train segment only."""
    train_w = w[segments == TRAIN].dropna()
    return train_w.quantile(0.10), train_w.quantile(0.90)


def assign_band(w, low, high):
    """Band from W(t), which is known at forecast time: low <= p10, flood >= p90, else normal."""
    band = pd.Series("normal", index=w.index, dtype="object")
    band[w <= low] = "low"
    band[w >= high] = "flood"
    band[w.isna()] = None
    return band


def count_episodes(times, gap):
    """Number of separate events: hours more than `gap` after the previous one start a new one."""
    times = pd.DatetimeIndex(times).sort_values()
    if len(times) == 0:
        return 0
    return 1 + int((times[1:] - times[:-1] > gap).sum())


def band_report(frames, evaluation):
    """Rows, episodes and evaluability per station, segment and band.

    frames: {station: DataFrame} indexed by origin hour t with the columns `w` (W at t),
    `y_delta_cm` (24 h target) and `segment`. Only rows with a target are counted; the band
    limits come from the train rows of the same station.

    The normal band is evaluable whenever it has rows, and is never pooled. For low and flood:
    a band is evaluable if its own episodes reach the minimum. If not and pooling is on, the
    same band and segment of all stations form one event set: the union of their origin hours,
    so one flood wave seen at three gauges counts once. The band is then evaluable if that set
    reaches the minimum, and `pooled` is True.
    """
    minimum = evaluation.min_episodes_per_band
    gap = evaluation.episode_gap

    # Origin hours per station, segment and band (rows with a target only).
    times = {}
    for station, frame in frames.items():
        low, high = band_thresholds(frame["w"], frame["segment"])
        band = assign_band(frame["w"], low, high)
        has_target = frame["y_delta_cm"].notna()
        for segment in SEGMENTS:
            for name in BANDS:
                keep = has_target & (frame["segment"] == segment) & (band == name)
                times[station, segment, name] = frame.index[keep]

    rows = []
    for (station, segment, name), own in times.items():
        union = pd.DatetimeIndex([])
        for other in frames:
            union = union.union(times[other, segment, name])
        episodes = count_episodes(own, gap)
        pooled_episodes = count_episodes(union, gap)
        if name in RARE_BANDS:
            own_ok = episodes >= minimum
            pooled_ok = evaluation.pool_thin_bands and pooled_episodes >= minimum
        else:
            own_ok = len(own) > 0
            pooled_ok = False
        rows.append(
            {
                "station": station,
                "segment": segment,
                "band": name,
                "rows": len(own),
                "episodes": episodes,
                "pooled_episodes": pooled_episodes,
                "evaluable": own_ok or pooled_ok,
                "pooled": (not own_ok) and pooled_ok,
            }
        )
    return pd.DataFrame(rows, columns=REPORT_COLUMNS)
