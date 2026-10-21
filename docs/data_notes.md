# BAFU data notes

First probe of the BAFU Hydrological Data Service on 08.10.2026, around 17:00 UTC.
Script: `scripts/probe_bafu.py` (`uv run python scripts/probe_bafu.py`).

## How the data is queried

- Endpoint: `https://data.bafu.admin.ch/api`, HTTP POST with a JSON body `{"query": ..., "variables": ...}`, no key.
- Station metadata: `water { observations { stations(where: { no: { _in: $nos } }) { no name riverName status coverageFrom coverageTo } } }`.
- Hourly values: `water { observations { data_1hour_mean(where: { station: { no: { _eq: $station } }, timestamp: { _gte: $from, _lt: $to } }) { timestamp parameterName value unitSymbol } } }`. Timestamps are ISO UTC strings (`AWSDateTime`), the station number is a string.
- One row per timestamp and parameter. A query that would return more than 10,000 rows is rejected, not cut off: HTTP 200 with `errors[0].message` "Query returned more than 10000 rows. Please narrow your query using filters or specify a limit." (tested 09.10.2026 with 5 months of Seedorf). The probe fetches one day per request (at most 72 rows). Errors come back in an `errors` field, also with HTTP 200, so the script checks for it.
- Rate limit (https://data.bafu.admin.ch/rate-limits): 500 requests per IP in a sliding 5-minute window. Above that the server answers HTTP 403 until the window drops below the limit. BAFU recommends fewer, larger requests, run one after the other, and a short pause before retrying a 403.

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

## Weather probe (20.10.2026, around 20:15 UTC)

Script: `scripts/probe_weather.py` (`uv run python scripts/probe_weather.py`, and `--poll --cycles 3` to time the delay). It calls Open-Meteo without a key, uses the coordinates from BAFU's `stations` query and stores nothing. Model `ecmwf_ifs` (IFS HRES, 9 km). Raw output of the last run: 81 requests.

### Locations

| No. | Name | Latitude | Longitude | Grid cell elevation (Open-Meteo) |
|---|---|---|---|---|
| 2087 | Andermatt | 46.6423 | 8.5897 | 1447 m |
| 2491 | Bürglen, Galgenwäldli | 46.8709 | 8.6518 | 491 m |
| 2056 | Seedorf | 46.8841 | 8.6205 | 441 m |

2491 and 2056 are about 3 km apart and fall into the same 9 km model cell. Precipitation and snowfall are identical for the two, only the air temperature differs by about 0.3 °C (downscaled to the point elevation). So the lower Reuss and the Schächen share one precipitation series. There are two independent weather points, not three.

### 1. Single Runs API

Request: `single-runs-api.open-meteo.com/v1/forecast?latitude=..&longitude=..&hourly=precipitation,temperature_2m,snowfall&models=ecmwf_ifs&run=2026-09-15T00:00&forecast_hours=48`.

- Returns 48 hourly rows, timestamps in UTC (`timezone: GMT`), the first row is the run's start hour. Tested on 16.04.2024, 14.01.2025 and 15.09.2026 for all four cycles and all three points.
- Units: precipitation in mm, temperature_2m in °C, snowfall in cm. Precipitation is the sum over the preceding hour.
- The precipitation value of the first row (the run's start hour) is `null` in every run, because there is no hour before the start. Temperature and snowfall have a value in the first row. A feature built from a run must not use hour 0 of precipitation.
- 48 hours per run is accepted. Answers are small (a few KB) and fast.
- A run that does not exist gives HTTP 400 with `{"error": true, "reason": "The requested model run is not available. Model: ecmwf_ifs, run: ...Z"}`. The client must check `error`, not only the status code.

**Start of the archive: 14.03.2024 is confirmed, but only for two of the four cycles.**

- Runs on 13.03.2024 and earlier: not available (error above). First run: 14.03.2024 00:00 UTC.
- On 14.03.2024 the 00Z and 12Z runs exist, the 06Z and 18Z runs do not.
- On 16.04.2024 again only 00Z and 12Z exist.
- 06Z and 18Z runs appear from 07.08.2024 (checked on the first day of each month and then day by day). From 14.01.2025 onwards all four cycles exist, as they do today.
- So the weather plan's "4 runs per day since 14.03.2024" is wrong for the first five months: 2 runs per day from 14.03.2024 to 06.08.2024 (146 days), 4 runs per day from 07.08.2024.

### 2. Availability delay

Where the answer is known now: tested today at 20:18 UTC. Runs 12Z and older were fetchable, the 18Z run (started 2.3 h earlier) was not. So the delay of a run is more than 2.3 h, and the 12Z run was out at the latest 8.3 h after its start.

Measured delay (polling every 5 minutes from the run's start, precision 5 minutes): done. A polling run for the 18Z (20.10), 00Z and 06Z (21.10) runs was started at 20:18 UTC and writes one line per run when it succeeds. 

| Run (UTC) | First successful fetch (UTC) | Delay |
|---|---|---|
| 2026-10-20 18:00 | 2026-10-20 23:43 | 344 min (5.7 h) |
| 2026-10-21 00:00 | 2026-10-21 05:55 | 355 min (5.9 h) |
| 2026-10-21 06:00 | 2026-10-21 11:50 | 350 min (5.8 h) |

All three runs came out 5.7 to 5.9 h after their start, so the delay looks stable. A run is usable about 6 h after its start. A cut-off of 7 h leaves a margin.

### 3. Historical Forecast API against Single Runs

`historical-forecast-api.open-meteo.com/v1/forecast` with `start_date`, `end_date` and `models=ecmwf_ifs` works with the same variables and units and returns one continuous hourly series (72 rows for 3 days). Differences to Single Runs for the same valid hours, per point and run:

| Lead time | 15.09.2026 | 14.01.2025 | 16.04.2024 |
|---|---|---|---|
| +1 to +5 h | identical (0.00) | identical for 00Z and 12Z; temperature differs by about 0.4 to 1.0 °C for 06Z and 18Z | differs: precipitation up to 0.7 mm per hour, temperature mean up to 1.2 °C |
| +24 to +47 h | precipitation mean 0.1 to 0.9 mm per hour (max 6.2), temperature mean 0.5 to 1.0 °C (max 3.9) | precipitation about 0 (dry), temperature mean 0.8 to 1.2 °C (max 3.4) | precipitation mean 0.7 to 0.8 mm per hour (max 2.7), snowfall mean up to 0.6 cm |

Reading: the Historical Forecast series is made of the first hours of the runs. In 2026 its early hours are the same numbers as the Single Runs early hours, and 24 to 47 h ahead they differ clearly. It therefore shows an analysis-like "what the weather was", not what a forecast said one day ahead. That fits the plan: Historical Forecast for the observed part, Single Runs for the forecast as issued. In 2024 even the early hours differ, so the composition of the stitched series changed over time. Observed precipitation for 2024 is therefore not exactly the same product as in 2026.

### 4. Live API against Historical Forecast API

`api.open-meteo.com/v1/forecast?...&models=ecmwf_ifs&past_days=3&forecast_days=1` returned 96 rows. For the three full days 17.10 to 19.10.2026 the live past hours are identical to the Historical Forecast API (differences 0.00 for precipitation, temperature and snowfall, 24 hours each, Andermatt point). The live API's past hours can therefore serve as the "observed" part in the hourly run. The Historical Forecast API had values up to the end of the requested day (with `end_date` = today it also returned today's coming hours), so no lag was visible.

### 5. Requests, timing, limits

- 81 requests: 54 with status 200, 27 with HTTP 400 (runs that do not exist, tested on purpose). No HTTP 429.
- Response time: mean 0.13 s, max 0.28 s. I waited 0.3 s between requests.
- No rate-limit headers (`X-RateLimit-*`, `Retry-After`) in any response. The limits are only known from the terms page: free tier, fewer than 10,000 calls per day, 5,000 per hour, 600 per minute, per IP.
- One request with three comma-separated locations works (`latitude=a,b,c&longitude=x,y,z`) and returns a list of three structures. The pages I read do not say whether this counts as one call or three against the daily limit. Assume three until it is clear.

Backfill estimate (14.03.2024 to 20.10.2026 = 951 days):

| Case | Runs | Requests |
|---|---|---|
| Plan: 4 runs per day since 14.03.2024, 3 points | 3,804 | 11,412 |
| Real cycles (2 runs per day to 06.08.2024, then 4), 3 points, one point per request | 3,512 | 10,536 |
| Same, but 2 points (2491 and 2056 share a cell) | 3,512 | 7,024 |
| Same, 3 locations per request | 3,512 | 3,512 (if one request counts as one call) |

At 10,000 per day the 3-point backfill fits in about two days, the 2-point backfill in one day. The hourly live run needs about 1 new run per cycle per point, so well below the limit.

### Source and licence

- Data: Open-Meteo.com (https://open-meteo.com), API data under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Credit must be given with a link to the licence, and changes must be indicated. Open-Meteo's example credit: "Weather data by Open-Meteo.com" with a link to https://open-meteo.com/. It goes into the README.
- Free use: the free API is for non-commercial use only (terms: https://open-meteo.com/en/terms). Allowed examples include educational content and non-profit projects without subscriptions or advertising. A course project fits.
- Limits on the free tier: see section 5. Open-Meteo asks to contact them above 10,000 requests per day and may block abusive use.
- Open-Meteo's own source code is AGPLv3. That does not affect this project, because only the API is used.
- The licence page https://open-meteo.com/en/license could not be read as plain text (it needs JavaScript). The wording above comes from the terms page and the project's GitHub page. Check the exact wording on the licence page before writing the README.
