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
