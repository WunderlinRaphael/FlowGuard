# BAFU data notes

First probe of the BAFU Hydrological Data Service on 08.10.2026, around 17:00 UTC.
Script: `scripts/probe_bafu.py` (`uv run python scripts/probe_bafu.py`).

## How the data is queried

- Endpoint: `https://data.bafu.admin.ch/api`, HTTP POST with a JSON body `{"query": ..., "variables": ...}`, no key.
- Station metadata: `water { observations { stations(where: { no: { _in: $nos } }) { no name riverName status coverageFrom coverageTo } } }`.
- Hourly values: `water { observations { data_1hour_mean(where: { station: { no: { _eq: $station } }, timestamp: { _gte: $from, _lt: $to } }) { timestamp parameterName value unitSymbol } } }`. Timestamps are ISO UTC strings (`AWSDateTime`), the station number is a string.
- One row per timestamp and parameter. A query returns at most 10,000 rows, so the probe fetches one day per request (at most 72 rows). Errors come back in an `errors` field, also with HTTP 200, so the script checks for it.

## Station metadata

| No. | Name (BAFU) | River (BAFU) | Status | Coverage from | Coverage to |
|---|---|---|---|---|---|
| 2087 | Andermatt | Reuss | Aufgebaut (active) | 1910-01-01 | 2026-10-08 |
| 2299 | Erstfeld, Bodenberg | Alpbach | active | 1960-01-01 | 2026-10-08 |
| 2491 | Bürglen, Galgenwäldli, nur Hauptstation | Schächen | active | 1985-01-01 | 2026-10-08 |
| 2492 | Bürglen, EW Altdorf | Dorfbachableitung | active | 1986-01-01 | 2026-10-08 |
| 2649 | Bürglen, KW Schächen | Ableitung EWA | active | 2020-01-01 | 2026-10-08 |
| 2056 | Seedorf | Reuss | active | 1904-01-01 | 2026-10-08 |


## Last 7 days of hourly means

Window 2026-10-01 17:00 to 2026-10-08 17:00 UTC, 168 expected hours. "Gaps" counts missing hours up to the latest value; the hours after the latest value are publication delay and are shown as "delay".

| No. | Q (m3/s) | W (m ü.M.) | WT (°C) | Rows per parameter | Gaps | Latest value (UTC) | Delay |
|---|---|---|---|---|---|---|---|
| 2087 | yes | yes | no | 165 | 1 | 2026-10-08 14:00 | 3 h |
| 2299 | yes | yes | no | 126 | 24 | 2026-10-07 22:00 | 19 h |
| 2491 | yes | yes | no | 165 | 1 | 2026-10-08 14:00 | 3 h |
| 2492 | yes | no | no | 165 | 1 | 2026-10-08 14:00 | 3 h |
| 2649 | yes | no | no | 165 | 1 | 2026-10-08 14:00 | 3 h |
| 2056 | yes | yes | yes | 165 | 1 | 2026-10-08 14:00 | 3 h |

- The single gap is the same hour (2026-10-08 01:00 UTC) for every parameter at 2087, 2491, 2492, 2649 and 2056, so it looks like a hole on the BAFU side, not at one gauge.
- 2299 (Alpbach) is missing a full block from 2026-10-04 23:00 to 2026-10-05 22:00 and its newest value is 19 hours old. This fits the BAFU note that the station has no real-time publication: its data seems to arrive in batches.
- Hourly means for the last 2 to 3 hours are not available yet.

## What this means for the spec

1. **W is in metres above sea level, not centimetres.** For example Andermatt reads about 1426.56 m ü.M. The value needs `× 100` for cm, and differences (deltas) are what matter for the 3 cm target.
2. **Only 4 of the 6 stations have a water level.** 2492 and 2649 deliver only discharge Q. They cannot be forecast in cm W as the spec says.
3. **Water temperature WT exists only at Seedorf (2056).** The planned WT features are missing for the other stations.
4. **2299 is not live.** About 19 hours of delay and a one-day hole in the last week. For a forecast made every hour, this station is often missing its newest values.
5. Seedorf (2056) is fine: coverageTo is today, and data is current. The 23.08.2026 date seen on the BAFU website was only a display issue.

## Second probe (09.10.2026, around 08:30 UTC)

The probe now also requests `releaseState` and compares `data_1hour_mean` with `data_10min_mean`. The station list and the 7-day check gave the same picture as on 08.10, except that 2299 had still not delivered anything after 2026-10-07 22:00 (34 hours old).

### Hour label: a timestamp marks the start of its hour

For 2087, 2491 and 2056 (Q, six hours each on 2026-10-08) the hourly mean was compared with two averages of the 10-minute means: the hour after the timestamp, [T, T+1h), and the hour before it, (T−1h, T].

| Station | Hours checked | Matches "hour after" | Largest difference to "hour after" |
|---|---|---|---|
| 2087 | 6 | 6 | 0.0003 m3/s |
| 2491 | 6 | 6 | 0.0005 m3/s |
| 2056 | 6 | 6 | 0.0007 m3/s |

The "hour before" was off by up to 3.6 m3/s (Seedorf at 07:00). So the hourly value with timestamp 10:00 is the mean of 10:00 to 10:59. It is only complete at 11:00.

### Release state

`releaseState` per station, counted over the last 7 days and on one sample day further back (hourly values). "None" means the field is empty.

| Station | Last 7 days | 30, 60, 90 days back | 180 days back | 365 days back | 730 days back |
|---|---|---|---|---|---|
| 2087 | None | None | 2 | 2 | 2 |
| 2299 | None | None | None | 2 | 2 |
| 2491 | None | None | None | 2 | 2 |
| 2492 | None | None | None | 2 | 2 |
| 2649 | None | None | None | 2 | 2 |
| 2056 | None | None | None for 48 rows, 2 for 24 | 2 | 2 for 48 rows, 3 for 24 |

Recent values have no release state at all (not even 1 = provisional). Validation reaches back roughly 6 to 12 months, depending on the station and parameter. Older values are validated (2), a few definitive (3).

### Age of the newest value

At 08:24 UTC the newest hourly value was 06:00 for 2087, 2491, 2492, 2649 and 2056: 2.4 hours after its timestamp, 1.4 hours after its hour ended. 2299 was at 2026-10-07 22:00, 34 hours old.

### What this means

1. **Timestamps are hour starts.** At forecast time t (the timestamp of the newest complete hour), the newest value covers t to t+1h. The label "W at t+24h" is the hourly value with timestamp t+24h, so it covers t+24h to t+25h. Features and label use the same convention, so there is no leakage as long as both come from `data_1hour_mean`.
2. **Hourly values are about 1.5 hours late after their hour ends.** For a run at hh:30, the newest complete hour is usually two hours earlier (timestamp hh−2).
3. **History and live data are not in the same state.** Training data older than about a year is validated by BAFU; the last 6 to 12 months and all live data are unchecked. Corrections made during validation (for example ice effects at Andermatt in winter) are in the training data but not in what the model sees live. This is a possible train/serve skew. The size of the corrections cannot be measured from one probe, because the API only returns the current version of each value.
4. 2299 stays observe only: still no data after 07.10.
