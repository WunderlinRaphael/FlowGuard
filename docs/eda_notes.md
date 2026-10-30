# EDA notes: persistence baseline and travel time

Snapshot: 2026-10-30 07:12 UTC, store `gs://flowguard-hs26-data`, 1,447,665 raw rows. Written by `scripts/eda_baseline.py`; rerun the script instead of editing this file.

Rows are hourly origins t and neighbouring rows are strongly correlated, so a row count overstates how much independent information a segment holds. The episode counts are the better guide. All numbers use the cleaned series (checks from `common/cleaning.py`).

## Cut dates used (provisional, UTC)

train until 2026-03-15, validation 2026-03-15 to 2026-06-15, test 2026-06-15 to 2026-10-01, embargo 24 h. First origin with weather features: 2024-08-10 00:00.

## Data volume (E3)

Raw BAFU hourly means in the store. `missing_percent` counts the gaps between the first and the last delivered hour of each series.

| station | parameter | first | last | hours | missing_percent |
|---|---|---|---|---|---|
| 2056 Seedorf | Q | 2011-01-01 00:00 | 2026-10-30 03:00 | 138748 | 0.00 |
| 2056 Seedorf | W | 2011-01-01 00:00 | 2026-10-30 03:00 | 138748 | 0.00 |
| 2056 Seedorf | WT | 2011-01-01 00:00 | 2026-10-30 03:00 | 138748 | 0.00 |
| 2087 Andermatt | Q | 2011-01-01 00:00 | 2026-10-30 03:00 | 138748 | 0.03 |
| 2087 Andermatt | W | 2011-01-01 00:00 | 2026-10-30 03:00 | 138748 | 0.00 |
| 2299 Erstfeld, Bodenberg | Q | 2011-01-01 00:00 | 2026-10-29 22:00 | 138743 | 1.30 |
| 2299 Erstfeld, Bodenberg | W | 2011-01-01 00:00 | 2026-10-29 22:00 | 138743 | 1.30 |
| 2491 Bürglen, Galgenwäldli | Q | 2011-01-01 00:00 | 2026-10-30 03:00 | 138748 | 0.00 |
| 2491 Bürglen, Galgenwäldli | W | 2011-01-01 00:00 | 2026-10-30 03:00 | 138748 | 0.00 |
| 2492 Bürglen, EW Altdorf | Q | 2011-01-01 00:00 | 2026-10-30 03:00 | 138748 | 0.00 |
| 2649 Bürglen, KW Schächen | Q | 2019-12-31 23:00 | 2026-10-30 03:00 | 59861 | 0.00 |

## Cleaning flags over the whole history

| station | parameter | range | spike | flat |
|---|---|---|---|---|
| 2056 Seedorf | Q | 0 | 2 | 0 |
| 2056 Seedorf | W | 0 | 1 | 0 |
| 2087 Andermatt | Q | 0 | 1 | 0 |
| 2087 Andermatt | W | 0 | 0 | 0 |
| 2299 Erstfeld, Bodenberg | Q | 0 | 1 | 0 |
| 2299 Erstfeld, Bodenberg | W | 0 | 1 | 0 |
| 2491 Bürglen, Galgenwäldli | Q | 0 | 2 | 0 |
| 2491 Bürglen, Galgenwäldli | W | 0 | 0 | 0 |
| 2492 Bürglen, EW Altdorf | Q | 0 | 0 | 0 |
| 2649 Bürglen, KW Schächen | Q | 0 | 0 | 0 |

## Risk band thresholds

10th and 90th percentile of W(t) in the train segment (m a.s.l.). A row is `low` at or below p10, `flood` at or above p90, otherwise `normal`. The band comes from W(t), which is known at forecast time.

| station | p10_w | p90_w |
|---|---|---|
| 2087 Andermatt | 1426.54 | 1427.01 |
| 2491 Bürglen, Galgenwäldli | 485.50 | 485.96 |
| 2056 Seedorf | 437.12 | 438.26 |

## Persistence baseline

MAE in cm of the rule W(t + 24 h) = W(t), only rows with a target and a segment label. `share_over_3cm` is the share of rows with an absolute 24 h change above 3 cm.

| station | segment | band | rows | mae_cm | share_over_3cm |
|---|---|---|---|---|---|
| 2087 Andermatt | train | all | 133224 | 3.68 | 0.26 |
| 2087 Andermatt | train | low | 13338 | 1.15 | 0.04 |
| 2087 Andermatt | train | normal | 106549 | 2.96 | 0.23 |
| 2087 Andermatt | train | flood | 13337 | 11.93 | 0.71 |
| 2087 Andermatt | val | all | 2184 | 4.04 | 0.37 |
| 2087 Andermatt | val | low | 190 | 3.15 | 0.12 |
| 2087 Andermatt | val | normal | 1949 | 3.80 | 0.38 |
| 2087 Andermatt | val | flood | 45 | 17.90 | 0.82 |
| 2087 Andermatt | test | all | 2568 | 3.02 | 0.20 |
| 2087 Andermatt | test | low | 0 | n/a | n/a |
| 2087 Andermatt | test | normal | 2548 | 2.69 | 0.19 |
| 2087 Andermatt | test | flood | 20 | 45.60 | 1.00 |
| 2491 Bürglen, Galgenwäldli | train | all | 133224 | 3.14 | 0.27 |
| 2491 Bürglen, Galgenwäldli | train | low | 13631 | 1.24 | 0.06 |
| 2491 Bürglen, Galgenwäldli | train | normal | 106159 | 3.02 | 0.26 |
| 2491 Bürglen, Galgenwäldli | train | flood | 13434 | 6.03 | 0.60 |
| 2491 Bürglen, Galgenwäldli | val | all | 2184 | 2.70 | 0.19 |
| 2491 Bürglen, Galgenwäldli | val | low | 1433 | 0.82 | 0.05 |
| 2491 Bürglen, Galgenwäldli | val | normal | 751 | 6.29 | 0.46 |
| 2491 Bürglen, Galgenwäldli | val | flood | 0 | n/a | n/a |
| 2491 Bürglen, Galgenwäldli | test | all | 2568 | 1.87 | 0.11 |
| 2491 Bürglen, Galgenwäldli | test | low | 626 | 0.66 | 0.04 |
| 2491 Bürglen, Galgenwäldli | test | normal | 1941 | 2.25 | 0.14 |
| 2491 Bürglen, Galgenwäldli | test | flood | 1 | 22.00 | 1.00 |
| 2056 Seedorf | train | all | 133222 | 10.98 | 0.73 |
| 2056 Seedorf | train | low | 13414 | 4.56 | 0.44 |
| 2056 Seedorf | train | normal | 106455 | 10.21 | 0.75 |
| 2056 Seedorf | train | flood | 13353 | 23.62 | 0.88 |
| 2056 Seedorf | val | all | 2184 | 9.77 | 0.73 |
| 2056 Seedorf | val | low | 472 | 4.44 | 0.49 |
| 2056 Seedorf | val | normal | 1618 | 10.53 | 0.79 |
| 2056 Seedorf | val | flood | 94 | 23.44 | 0.90 |
| 2056 Seedorf | test | all | 2568 | 11.96 | 0.79 |
| 2056 Seedorf | test | low | 49 | 9.07 | 0.78 |
| 2056 Seedorf | test | normal | 2485 | 11.31 | 0.79 |
| 2056 Seedorf | test | flood | 34 | 63.89 | 1.00 |

- 2056 Seedorf, test: overall MAE 11.96 cm, 0 of 1 bands with at least 100 rows at or below 3 cm. Persistence is above 3 cm in most bands: 3 cm is a real target here.
- 2056 Seedorf, val: overall MAE 9.77 cm, 0 of 2 bands with at least 100 rows at or below 3 cm. Persistence is above 3 cm in most bands: 3 cm is a real target here.
- 2087 Andermatt, test: overall MAE 3.02 cm, 1 of 1 bands with at least 100 rows at or below 3 cm. Persistence is already at or below 3 cm in most bands: 3 cm is easy here.
- 2087 Andermatt, val: overall MAE 4.04 cm, 0 of 2 bands with at least 100 rows at or below 3 cm. Persistence is above 3 cm in most bands: 3 cm is a real target here.
- 2491 Bürglen, Galgenwäldli, test: overall MAE 1.87 cm, 2 of 2 bands with at least 100 rows at or below 3 cm. Persistence is already at or below 3 cm in most bands: 3 cm is easy here.
- 2491 Bürglen, Galgenwäldli, val: overall MAE 2.70 cm, 1 of 2 bands with at least 100 rows at or below 3 cm. Persistence is at or below 3 cm in half of the bands.

## Events per segment

Rows with a target per band, and `episodes`: separate events (band hours more than 24 h apart count as different events). A band under 100 rows is thin.

| station | segment | band | rows | episodes |
|---|---|---|---|---|
| 2087 Andermatt | train | low | 13338 | 88 |
| 2087 Andermatt | train | normal | 106549 | 160 |
| 2087 Andermatt | train | flood | 13337 | 179 |
| 2087 Andermatt | val | low | 190 | 4 |
| 2087 Andermatt | val | normal | 1949 | 3 |
| 2087 Andermatt | val | flood | 45 | 5 |
| 2087 Andermatt | test | low | 0 | 0 |
| 2087 Andermatt | test | normal | 2548 | 1 |
| 2087 Andermatt | test | flood | 20 | 3 |
| 2491 Bürglen, Galgenwäldli | train | low | 13631 | 93 |
| 2491 Bürglen, Galgenwäldli | train | normal | 106159 | 153 |
| 2491 Bürglen, Galgenwäldli | train | flood | 13434 | 174 |
| 2491 Bürglen, Galgenwäldli | val | low | 1433 | 5 |
| 2491 Bürglen, Galgenwäldli | val | normal | 751 | 6 |
| 2491 Bürglen, Galgenwäldli | val | flood | 0 | 0 |
| 2491 Bürglen, Galgenwäldli | test | low | 626 | 14 |
| 2491 Bürglen, Galgenwäldli | test | normal | 1941 | 6 |
| 2491 Bürglen, Galgenwäldli | test | flood | 1 | 1 |
| 2056 Seedorf | train | low | 13414 | 182 |
| 2056 Seedorf | train | normal | 106455 | 149 |
| 2056 Seedorf | train | flood | 13353 | 202 |
| 2056 Seedorf | val | low | 472 | 1 |
| 2056 Seedorf | val | normal | 1618 | 7 |
| 2056 Seedorf | val | flood | 94 | 3 |
| 2056 Seedorf | test | low | 49 | 6 |
| 2056 Seedorf | test | normal | 2485 | 1 |
| 2056 Seedorf | test | flood | 34 | 3 |

Thin bands (under 100 rows) in validation or test:

- 2087 Andermatt, val, flood: 45 rows
- 2087 Andermatt, test, low: 0 rows
- 2087 Andermatt, test, flood: 20 rows
- 2491 Bürglen, Galgenwäldli, val, flood: 0 rows
- 2491 Bürglen, Galgenwäldli, test, flood: 1 rows
- 2056 Seedorf, val, flood: 94 rows
- 2056 Seedorf, test, low: 49 rows
- 2056 Seedorf, test, flood: 34 rows

- Best move of `train_end` (and its neighbour): to 2025-09-21 (-25 weeks), thin bands 8 -> 7.
- Best move of `val_end` (and its neighbour): to 2026-05-25 (-3 weeks), thin bands 8 -> 7.

## Train events, all rows and weather era

Train segment only. The second table counts only origins from 2024-08-10 00:00, the first origin of the model with weather.

All rows:

| station | band | rows | episodes |
|---|---|---|---|
| 2087 Andermatt | low | 13338 | 88 |
| 2087 Andermatt | normal | 106549 | 160 |
| 2087 Andermatt | flood | 13337 | 179 |
| 2491 Bürglen, Galgenwäldli | low | 13631 | 93 |
| 2491 Bürglen, Galgenwäldli | normal | 106159 | 153 |
| 2491 Bürglen, Galgenwäldli | flood | 13434 | 174 |
| 2056 Seedorf | low | 13414 | 182 |
| 2056 Seedorf | normal | 106455 | 149 |
| 2056 Seedorf | flood | 13353 | 202 |

From the first weather origin on:

| station | band | rows | episodes |
|---|---|---|---|
| 2087 Andermatt | low | 2895 | 11 |
| 2087 Andermatt | normal | 10741 | 29 |
| 2087 Andermatt | flood | 308 | 15 |
| 2491 Bürglen, Galgenwäldli | low | 7882 | 28 |
| 2491 Bürglen, Galgenwäldli | normal | 5881 | 28 |
| 2491 Bürglen, Galgenwäldli | flood | 181 | 13 |
| 2056 Seedorf | low | 2955 | 22 |
| 2056 Seedorf | normal | 10600 | 11 |
| 2056 Seedorf | flood | 389 | 19 |

## Release state

Share of rows (with a target) whose W value has an empty release state, for the forecast hour t and for the target hour t + 24 h. Empty means not yet validated.

| station | segment | rows | empty_at_t | empty_at_t_plus_24h |
|---|---|---|---|---|
| 2087 Andermatt | train | 133224 | 0.01 | 0.01 |
| 2087 Andermatt | val | 2184 | 0.67 | 0.67 |
| 2087 Andermatt | test | 2568 | 1.00 | 1.00 |
| 2491 Bürglen, Galgenwäldli | train | 133224 | 0.01 | 0.01 |
| 2491 Bürglen, Galgenwäldli | val | 2184 | 1.00 | 1.00 |
| 2491 Bürglen, Galgenwäldli | test | 2568 | 1.00 | 1.00 |
| 2056 Seedorf | train | 133222 | 0.01 | 0.01 |
| 2056 Seedorf | val | 2184 | 0.60 | 0.60 |
| 2056 Seedorf | test | 2568 | 1.00 | 1.00 |

## Travel time

Correlation of the hourly change of the upstream series at t - k with the hourly change at Seedorf (2056) at t, train segment only, k = 0 to 24 h.

| pair | parameter | best_lag_h | corr_at_best | lag_0h | lag_3h | lag_6h | lag_9h | lag_12h |
|---|---|---|---|---|---|---|---|---|
| 2087 -> 2056 | W | 2 | 0.21 | 0.13 | 0.16 | 0.00 | -0.03 | -0.02 |
| 2491 -> 2056 | W | 1 | 0.16 | 0.14 | 0.05 | -0.01 | -0.02 | 0.01 |
| 2087 -> 2056 | Q | 2 | 0.33 | 0.14 | 0.21 | -0.01 | -0.02 | -0.04 |
| 2491 -> 2056 | Q | 1 | 0.25 | 0.24 | 0.09 | -0.02 | -0.04 | -0.02 |
| 2492 -> 2056 | Q | 9 | 0.01 | -0.01 | -0.00 | 0.00 | 0.01 | 0.00 |
| 2649 -> 2056 | Q | 1 | 0.11 | 0.08 | 0.11 | -0.03 | -0.07 | 0.01 |

Same with changes over 6 h instead of 1 h (less noise from hydropeaking):

| pair | parameter | best_lag_h | corr_at_best | lag_0h | lag_3h | lag_6h | lag_9h | lag_12h |
|---|---|---|---|---|---|---|---|---|
| 2087 -> 2056 | W | 2 | 0.52 | 0.44 | 0.47 | 0.16 | -0.07 | -0.09 |
| 2491 -> 2056 | W | 1 | 0.46 | 0.44 | 0.34 | 0.04 | -0.08 | -0.06 |
| 2087 -> 2056 | Q | 2 | 0.63 | 0.50 | 0.57 | 0.19 | -0.08 | -0.12 |
| 2491 -> 2056 | Q | 1 | 0.61 | 0.60 | 0.44 | 0.06 | -0.12 | -0.13 |
| 2492 -> 2056 | Q | 13 | 0.02 | -0.05 | -0.02 | -0.00 | 0.01 | 0.02 |
| 2649 -> 2056 | Q | 2 | 0.32 | 0.25 | 0.28 | -0.02 | -0.16 | -0.02 |

## Diversion flows 2492 and 2649

Hourly Q in the train segment, hours where both have a value. Fit: Q2649 = slope * Q2492 + intercept.

- hours: 54337
- corr_levels: 0.05
- corr_hourly_changes: -0.00
- slope: 2.02
- intercept: 1.55
- r2: 0.00
- share_2649_at_least_2492: 0.82
- median_2649_minus_2492: 1.86

## Weather runs not in the store

11 expected runs are not stored: run-20240808T06, run-20240809T06, run-20250804T18, run-20250805T00, run-20250805T06, run-20250806T00, run-20250807T18, run-20250808T00, run-20250808T12, run-20250809T00, run-20260610T12. Open-Meteo does not have these runs (the backfill skipped them with a warning).

Not counted: 1 run(s) from the last 12 hours, which the hourly ingest may not have fetched yet: run-20261030T00.
