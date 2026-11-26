# Evaluation rules

Fixed before any risk band is scored. The values are in the `split` and `evaluation` sections of `config/pipeline.yaml`; `src/flowguard/training/bands.py` implements the counting.

## Success criterion

- **Primary check, every station:** the overall MAE in cm and its ratio to persistence (prediction for t+24h = W at t) are always reported. The model has to beat persistence at every station.
- **Andermatt (2087):** additionally an overall MAE of at most 3 cm.
- **Seedorf (2056):** additionally an overall MAE of at most 0.8 of the persistence MAE.
- **Bands:** a claim about a risk band (low, normal, flood) is required only for evaluable bands (see below). A band that is not evaluable is reported with its rows and episodes and is not part of the criterion.
- **Segment:** the overall MAE targets (beat persistence at every station, 3 cm at Andermatt, at most 0.8 of persistence at Seedorf) are judged on the test segment, scored once. Validation is used for model selection only.

## Bands and episodes

- The band comes from W(t), which is known at forecast time: `low` at or below the 10th percentile, `flood` at or above the 90th, otherwise `normal`. The percentiles are computed per station from the train segment only.
- Only rows with a target (W at t+24h present) are counted, per station, segment and band.
- An episode is a group of band hours; band hours more than `episode_gap_hours` (24) apart are separate episodes. This is the definition used in `scripts/eda_baseline.py`; a test checks that both give the same counts.

## When a band is evaluable

The episode rule applies to the low and flood bands. The normal band is the bulk of the data and has few long episodes, so it is always scored when it has rows (never pooled). A band with zero rows is not evaluable.

Table for low and flood:

| Case | evaluable | pooled |
|---|---|---|
| The station has at least `min_episodes_per_band` (5) episodes in the band and segment | yes | no |
| Fewer, `pool_thin_bands` is true, and the pooled set has at least 5 episodes | yes | yes |
| Fewer, and the pooled set has fewer than 5 episodes (or pooling is off) | no | no |

The pooled set is the union of the origin hours of that band and segment over the three target stations, then counted with the same gap rule. One flood wave over all three gauges counts once; this is the stricter rule (summing the per-station episodes would count it up to three times). A pooled band is scored as one event set over the three stations.

## Split

The cut dates are frozen (decision 30.10.2026) and unchanged since they were first committed on 29.10.2026: train until 2026-03-15, validation 2026-03-15 to 2026-06-15, test 2026-06-15 to 2026-10-01, embargo 24 h. Moving a date needs a new decision and a note in the milestone summary; `tests/test_pipeline_config.py` pins the values.

`docs/eda_notes.md` was generated before the freeze was written down and still says "provisional"; the dates in it are the ones that are now frozen.
