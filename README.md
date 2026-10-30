# FlowGuard

FlowGuard forecasts the water level of the Reuss and the Schächen in canton Uri, 24 hours ahead, and turns each forecast into a risk band: low flow, normal or flood risk. Most flood tools look only at floods. I also want to catch the other end, droughts, which get less attention. The project is part of the HSLU module MLOps (HS26). It is a live system in the feature, training and inference (FTI) style, and it runs on a schedule.

## Stations

The data comes from the Swiss Federal Office for the Environment (BAFU). I forecast three stations and use three more as inputs or only collect them. I cut the original list of six after probing the data, and `docs/data_notes.md` shows why.

| BAFU no. | Station | Role | Why |
| --- | --- | --- | --- |
| 2087 | Reuss, Andermatt | Forecast | Water level and discharge, live. Upper end of the Reuss |
| 2491 | Schächen, Bürglen | Forecast | Water level and discharge, live |
| 2056 | Reuss, Seedorf | Forecast | Water level, discharge and temperature, live. Receives the other stations |
| 2492, 2649 | Diversions at Bürglen | Input | Discharge only, follows plant operation, so no forecast |
| 2299 | Alpbach, Erstfeld | Only collected | Not live, newest values arrive a day or more late |

## Status

The station config, the pipeline config, the BAFU client, the weather client, the hourly grid with gap report, the raw store, the backfill, the hourly ingest, the cleaning checks and the chronological split (all with tests) and two data probes (BAFU and Open-Meteo) are done. The Dockerfile, the CI workflow and the manual ingest and backfill workflows are done. The hourly ingest runs on a schedule. The feature table is not built yet.

## Data

- **Water data:** BAFU hydrological data service (GraphQL API, no login). I use the hourly means of discharge Q and water level W, and temperature WT at Seedorf. BAFU rejects queries over 10,000 rows, so the client asks in 30-day windows. Fallbacks: BAFU's monthly bulk files, hydrodaten.admin.ch and existenz.ch.
- **Weather data:** Open-Meteo, with ECMWF forecasts as they were issued, so the model never sees a forecast that did not exist yet. Weather data by [Open-Meteo.com](https://open-meteo.com/), licensed CC BY 4.0, used non-commercially.

## How it works

- **Feature pipeline:** every hour a GitHub Actions job pulls new BAFU and weather data, builds the features and writes them as versioned Parquet files. The feature code lives once in `common/` and is used by training and inference.
- **Training pipeline:** a weekly or manual job reads the stored features, trains one pooled LightGBM model for all three stations and logs it in MLflow.
- **Inference:** a service on Google Cloud Run loads the registered model, forecasts the level 24 hours ahead and shows the risk band on a dashboard.
- **Target:** the change of the level over 24 hours in cm, added to the current level. The goal is a mean absolute error of at most 3 cm per station, better than the simple baseline that tomorrow equals today.
- **Risk bands:** low below the station's own 10th percentile, flood risk above the 90th, normal in between. The percentiles come from the training period only.
- **Split:** three separate time segments (train, validation, test), cut at the same date for all stations, never shuffled.

## Why this stack

- **GitHub Actions** gives me the hourly schedule and the tests in the same place as the code, and it is free for this size.
- **Docker** gives the same environment on my laptop, in CI and on Cloud Run.
- **LightGBM** is a strong, fast model for tabular features and handles missing values, which happen with live data.
- **MLflow** tracks experiments and holds the model registry. For now it runs on a local file store. \[Update once the hosting is decided.\]
- **Google Cloud (Cloud Run, Cloud Storage)** carries the $50 course credit.

## Known limits

- BAFU validates data only months later. The model trains mostly on validated values and runs on unchecked ones. I keep the release state of every row and report the unvalidated part of the test period separately.
- Weather forecasts as issued exist only from 2024, so the weather model trains on about 2.2 years.
- About 500 km2 of the Reuss catchment between Andermatt and Seedorf has no gauge. Weather points stand in for it.
- Three forecast stations are few, so results for single extreme events are uncertain.

## Setup and run

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repository url>
cd FlowGuard
uv sync --locked
```

Tests and style checks (no network and no key needed):

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Data probes (need internet, no key). They print to the terminal and store nothing:

```bash
uv run python scripts/probe_bafu.py      # BAFU stations, gaps, release states, takes a few minutes
uv run python scripts/probe_weather.py   # Open-Meteo runs, delay, request counts
```

`probe_weather.py --poll --cycles 3` waits for the next model runs and measures their delay, which takes hours.

Fetch raw hourly values from BAFU (UTC datetimes, nothing is stored):

```python
from datetime import UTC, datetime
from flowguard.common.stations import load_stations
from flowguard.features.bafu_client import BafuClient

rows = BafuClient().fetch_stations(
    load_stations(), datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 20, tzinfo=UTC)
)
```

Run it with `uv run python`. Each row is a dict with station, timestamp, parameter, value, unit, release_state and fetched_at.

Turn the raw rows into a regular hourly UTC grid and print the gap report (nothing is stored). Gaps stay missing, nothing is filled:

```python
from flowguard.features.gap_report import format_report, gap_report
from flowguard.features.hourly_grid import build_hourly_grid

result = build_hourly_grid(rows)  # result.grid is a pandas DataFrame, result.duplicates a count
print(format_report(gap_report(result.grid, result.duplicates)))
```

Fetch raw weather from Open-Meteo (no key, nothing is stored). The settings come from `config/pipeline.yaml`. `select_run` picks the forecast run that was available for the origin hour `t`, from `t` and the config only:

```python
from datetime import UTC, datetime, timedelta
from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.features.weather_client import WeatherClient, select_run

config = load_pipeline_config()
t = datetime(2026, 10, 21, 10, tzinfo=UTC)
run = select_run(t, as_of_offset=config.as_of_offset, min_run_age=config.min_run_age)

client = WeatherClient()
forecast = client.fetch_run(config.weather_points, run)  # 48 h run, hour 0 left out
observed = client.fetch_observed(
    config.weather_points, t - timedelta(hours=72), t + timedelta(hours=1)
)
```

Each row is a dict with point, valid_time, run_start (empty for observed hours), parameter, value, unit and fetched_at.

Store raw data (append-only Parquet in `data/`, which git ignores). `--store` takes a local folder or, later, a `gs://` URI. Both commands need internet, no key:

```bash
uv run python -m flowguard.features.backfill --dry-run   # request counts, fetches nothing
uv run python -m flowguard.features.backfill             # BAFU from 2011-01-01, weather runs from 07.08.2024
uv run python -m flowguard.features.ingest               # last 48 h of BAFU, new weather runs, gap report
```

The backfill is resumable: it skips what is already stored, so after an error or a stop (Ctrl+C) run the same command again. Options: `--limit N` (at most N requests per source), `--source bafu|weather|all`, `--bafu-start` and `--weather-start` (YYYY-MM-DD, override `config/pipeline.yaml`, for small tests). The ingest can be run as often as you like: it writes only new or changed BAFU rows and weather runs that are not stored yet. Files are never overwritten:

```
data/raw/bafu/ingest_date=YYYY-MM-DD/<unit>__<fetch time>__<id>.parquet
data/raw/weather/run_date=YYYY-MM-DD/<unit>__<fetch time>__<id>.parquet
```

The store location comes from `--store` or, if that is not given, from the environment variable `FLOWGUARD_STORE` (a folder or a `gs://` URI), else `data/`. Copy `.env.example` to `.env` for a local value (`.env` is ignored by git).

### Docker

The image runs the hourly ingest by default. The backfill runs from the same image by changing the entry point:

```bash
docker build -t flowguard .
docker run --rm -v "$PWD/data:/app/data" flowguard --store data
docker run --rm -v "$PWD/data:/app/data" --entrypoint python flowguard -m flowguard.features.backfill --dry-run
```

### GitHub Actions

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `ci.yml` | every push and pull request | `uv sync --locked`, ruff check and format check, pytest, and a Docker build without push |
| `ingest.yml` | manual (Actions tab, "Run workflow") | the hourly ingest into the GCS bucket |
| `backfill.yml` | manual, input `source` = `bafu` or `weather` | the resumable backfill into the GCS bucket (timeout 5.5 h) |

`ingest.yml` and `backfill.yml` need this in the repository settings (Settings > Secrets and variables > Actions):

| Name | Type | Content |
| --- | --- | --- |
| `GCP_SA_KEY` | secret | JSON key of the service account that may write to the bucket |
| `FLOWGUARD_STORE` | variable | bucket URI, `gs://flowguard-hs26-data` |
| `GCP_PROJECT` | variable | Google Cloud project ID, `flowguard-hs26` |

Both workflows have a concurrency group, so two runs of the same workflow never overlap. If a backfill stops (timeout or HTTP 429), start it again with the same source: it continues with the units that are not stored yet. The feature table, training and inference do not exist yet.

## Project layout

| Path | Content |
| --- | --- |
| `src/flowguard/common/` | Station list loader (`stations.py`), pipeline settings loader (`pipeline_config.py`), append-only raw store (`raw_store.py`), range, spike and flat-line checks with a causal mode for features (`cleaning.py`), chronological split with embargo (`split.py`), lag and rolling features per target station (`features.py`, W and Q of the station, its upstream slots and the diversion flows, plus the 24 h target) |
| `src/flowguard/features/` | BAFU GraphQL client (`bafu_client.py`), Open-Meteo weather client (`weather_client.py`), hourly grid (`hourly_grid.py`), gap report (`gap_report.py`), backfill command (`backfill.py`), hourly ingest command (`ingest.py`) |
| `src/flowguard/training/`, `src/flowguard/inference/` | Planned, empty for now |
| `config/` | `stations.yaml`: stations, roles, parameters and upstream mapping. `pipeline.yaml`: start dates, as-of rule, weather points, cleaning thresholds and split dates |
| `scripts/` | One-off probes of the BAFU and Open-Meteo APIs |
| `data/` | Raw Parquet files written by the backfill and ingest, not in git |
| `tests/` | Unit tests and recorded BAFU and Open-Meteo responses (`fixtures/`) |
| `docs/` | `data_notes.md` with the probe results, `proposal.pdf` |
| `notebooks/`, `ui/` | Planned, empty for now |
| `.github/workflows/` | `ci.yml`, `ingest.yml`, `backfill.yml` |
| `Dockerfile`, `.dockerignore` | Two-stage image (python:3.12-slim, uv, non-root user), default command is the ingest |
| `pyproject.toml`, `uv.lock` | Dependencies, pinned |
| `.env.example` | Template for environment variables (`FLOWGUARD_STORE`) |

## Related work

&#91;Four KTH ID2223 water-level projects and the difference to FlowGuard. Write this late, see the review response E1.\]
