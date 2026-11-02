# Feature table

One row per target station (2087 Andermatt, 2491 Bürglen, 2056 Seedorf) and origin hour `t`.
All stations have the same 156 columns in the same order. The table is built by
`flowguard.common.feature_table.build_feature_table` (BAFU part: `common/features.py`, weather
part: `common/weather_features.py`) and stored by `flowguard.features.build`.

## Time rules

- `t` is the start of an hour in UTC, as BAFU stamps it. A feature at `t` uses values stamped `t` or earlier.
- Lags and windows are looked up by timestamp on a regular hourly grid. A missing hour gives a missing value, never a shifted one.
- A station's rows run from the first to the last delivered hour of its own W or Q series. The newest 24 rows have no target and stay in the table; training drops rows without a target.
- The split into train, validation and test is not stored. It is assigned at training time from `config/pipeline.yaml`.
- Levels (W) are in cm (BAFU delivers m a.s.l., multiplied by 100), discharge (Q) in m3/s.

## Cleaning

Checks flag values, a flagged value becomes missing, raw data is never changed (`common/cleaning.py`, thresholds in `config/pipeline.yaml`).
- Features use the causal mode: range check, and a flat-line check that flags only the hour completing a run of `flat_hours` identical values and the hours after it. Spikes are not masked. A feature at `t` therefore never depends on later values.
- The target uses the full mode (range, spike, flat run as a whole) for W at `t+24h`, so a spike at `t+24h` gives a missing target.
- The manifest counts the flags of the full mode.

## Columns

### Metadata (not features)

| Name | Meaning |
| --- | --- |
| `station_id` | station number as string, `"2087"`, `"2491"`, `"2056"` (categorical for the model) |
| `t` | origin hour, UTC |
| `w_release_state` | BAFU release state of W at `t` (empty = not yet validated) |
| `y_release_state` | release state of W at `t+24h` |

### BAFU features

For each series prefix there are 14 columns. `<p>` is the prefix.

| Name | Meaning | Window |
| --- | --- | --- |
| `<p>_now` | value at `t` | |
| `<p>_d1h`, `_d6h`, `_d24h`, `_d72h`, `_d7d` | value at `t` minus the value 1 h, 6 h, 24 h, 72 h, 7 d earlier | point lags |
| `<p>_roll24_mean`, `_std`, `_min`, `_max` | statistics over the last 24 hours including `t` (std with ddof 1) | (t-24h, t] |
| `<p>_roll72_mean`, `_std`, `_min`, `_max` | the same over 72 hours | (t-72h, t] |

A difference is missing if one of its two values is missing. A rolling statistic is missing if fewer than 75 percent of the hours are present (18 of 24, 54 of 72).

| Prefix | Series | Slot |
| --- | --- | --- |
| `w`, `q` | W and Q of the station itself | own |
| `up_main_w`, `up_main_q` | W and Q of the main-river upstream station | `up_main` |
| `up_trib_w`, `up_trib_q` | W and Q of the tributary upstream station | `up_trib` |
| `div_q_2492`, `div_q_2649` | Q of the diversion stations EW Altdorf and KW Schächen | `div_q` |

The slots come from `config/stations.yaml`. A slot a station does not have gives missing columns (only Seedorf has upstream and diversion stations, so all `up_main_*`, `up_trib_*` and `div_q_*` columns are missing for Andermatt and Bürglen).

### Calendar

| Name | Meaning |
| --- | --- |
| `doy_sin`, `doy_cos` | sine and cosine of the day of the year (period 365.25 days) |

### Weather features

Per catchment point (`andermatt`, `meiental`, `schaechental`, coordinates in `config/pipeline.yaml`) 12 columns named `wx_<point>_<suffix>`. All stations get the columns of all points. Weather comes from the newest ECMWF IFS run that started at least 7 h before `t + 3 h`. The series seen at `t` is a composite of runs: leads 1 to 6 of every run up to the chosen run for the hours up to 6 h after that run's start, the chosen run itself after that. An Open-Meteo value covers the hour that ends at its valid time. Observed-like windows end with the hour of `t`, forecast windows start with the next hour. Windows are strict: one missing hour makes the value missing.

| Suffix | Meaning | Window |
| --- | --- | --- |
| `prec_obs_24h`, `prec_obs_48h`, `prec_obs_72h` | precipitation sum (mm) | last 24, 48, 72 h |
| `tmean_obs_24h`, `tmax_obs_24h` | mean and maximum temperature (°C) | last 24 h |
| `prec_fc_next6h`, `prec_fc_next12h`, `prec_fc_next24h` | forecast precipitation sum | next 6, 12, 24 h |
| `prec_fc_peak24h` | largest hourly forecast precipitation | next 24 h |
| `tmean_fc_24h`, `tmax_fc_24h` | forecast mean and maximum temperature | next 24 h |
| `snow_fc_next24h` | forecast snowfall sum (cm) | next 24 h |

`wx_run_age_h`: age in hours of the run actually used, `t` minus its start (4 to 9 h normally, more if the chosen run is missing). One column for all points.

Weather columns are missing for origins before `first_weather_origin` (`weather_start` + 72 h, 2024-08-10 00:00 UTC), because the weather archive has all four daily cycles only from 2024-08-07 and the longest window is 72 h. A run that is not stored, or has no value for a variable, is replaced by the newest older stored run with a value, up to 47 h lead. An hour no run covers is missing.

### Target

| Name | Meaning |
| --- | --- |
| `y_delta_cm` | (W at `t+24h` minus W at `t`) in cm. Forecast = W(t) + predicted delta. Persistence = 0. Missing if either value is missing or flagged, and for the newest 24 hours |

## Storage

```
features/<feature_version>/station=<number>/month=<YYYY-MM>/features.parquet
features/<feature_version>/_manifest.json
```

The month is the month of `t` (UTC). The file name is fixed. The store path is a local folder or a `gs://` URI (`--store` or `FLOWGUARD_STORE`), the same as for raw data. The Arrow schema is explicit: `station_id` and the release states are strings, `t` is `timestamp[us, UTC]`, everything else is float64. Files are written under a `.tmp` name and renamed. A month file is only written if its bytes differ from the stored file, so a second run of the same build writes nothing and the bucket collects no new object versions. Raw data (`raw/`) is append-only and is the source of truth; the feature table can always be rebuilt from it.

## Versioning

`feature_version` is one value in `config/pipeline.yaml` and part of the path. Three layers:
1. The version in the path is the schema version. It must be raised (v2) when feature names, definitions, windows, the cleaning thresholds, the weather points or the run selection rule change. v2 is built next to v1; v1 is deleted once training uses v2. A pure bug fix that changes values but not names or definitions is a rebuild of the same version.
2. The bucket has object versioning, so an overwritten month file keeps its old version for a rollback.
3. The manifest describes what was built (below). Training logs it with the model, so it identifies the dataset of every model.

## Rebuild

```bash
uv run python -m flowguard.features.build --store <folder or gs://...>                 # everything
uv run python -m flowguard.features.build --store <folder or gs://...> --since 2026-10-01
```

- Without `--since` the whole raw store is read and every month is rebuilt. This picks up BAFU corrections in old months. Do it before every milestone and after the December and January re-fetches (about 25 s for the 15-year history on a laptop with local files).
- `--since D` rebuilds the months from the month of `D` onward. Raw data is read from 22 days before the start of that month: 7 days for the longest lag, plus the longest flat-line window of the cleaning config (336 h = 14 days), plus one day. The value is derived from the config (`build.lookback`), so a longer flat window extends it. The rebuilt rows are byte-identical to the same rows of a full build (tested).
- The content of the table depends only on the raw data, never on the clock. Only `built_at` in the manifest does.
- GitHub Actions: workflow `Features` (manual, optional `since` input).
- Hourly build: after each ingest, the job `features` of `ingest.yml` runs `build --since <first day of the previous month>`, computed in the shell (UTC). So the current and the previous month are rebuilt, from raw data read 22 days before that start. This catches late BAFU values and the newest weather runs within the hour. Month files whose bytes did not change are not rewritten, so most runs write only the current month. Older months change only through a manual full build.

## Manifest

`_manifest.json` is rewritten at the end of every build.

| Field | Meaning |
| --- | --- |
| `feature_version` | version from the config |
| `built_at` | UTC time of the build |
| `code_version` | commit (`GITHUB_SHA` in Actions), otherwise `local` |
| `newest_fetched_at` | newest `fetched_at` read from BAFU and from weather raw data (`bafu`, `weather`) |
| `stations` | per station: `rows`, `rows_with_target`, `rows_with_weather` (all weather columns present), `first_t`, `last_t` |
| `cleaning_flags` | per series (`<station>/<W or Q>`): number of `range`, `spike` and `flat` hours in the full check. Series without a flag are not listed |
| `months`, `flag_months` | the same counts per station and month. An incremental build replaces the months it rebuilt and keeps the rest, so the totals stay valid |

Use: the manifest is the dataset version of a model (training logs it), and it shows how fresh the table is (`newest_fetched_at`, `last_t`) and what was dropped by cleaning.
