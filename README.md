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

The station config, the pipeline config, the BAFU client, the weather client, the hourly grid with gap report (all with tests) and two data probes (BAFU and Open-Meteo) are done. The features, the storage and the scheduled run are not built yet.

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

No environment variables are needed yet. `.env.example` is a template for later, and `.env` is ignored by git. The feature pipeline, training, inference, the Docker image and the CI workflows do not exist yet.

## Project layout

| Path | Content |
| --- | --- |
| `src/flowguard/common/` | Station list loader (`stations.py`), pipeline settings loader (`pipeline_config.py`). Shared feature code will live here |
| `src/flowguard/features/` | BAFU GraphQL client (`bafu_client.py`), Open-Meteo weather client (`weather_client.py`), hourly grid (`hourly_grid.py`), gap report (`gap_report.py`) |
| `src/flowguard/training/`, `src/flowguard/inference/` | Planned, empty for now |
| `config/` | `stations.yaml`: stations, roles, parameters and upstream mapping. `pipeline.yaml`: start dates, as-of rule and weather points |
| `scripts/` | One-off probes of the BAFU and Open-Meteo APIs |
| `tests/` | Unit tests and recorded BAFU and Open-Meteo responses (`fixtures/`) |
| `docs/` | `data_notes.md` with the probe results, `proposal.pdf` |
| `notebooks/`, `ui/` | Planned, empty for now |
| `.github/workflows/` | Planned, empty for now |
| `pyproject.toml`, `uv.lock` | Dependencies, pinned |
| `.env.example` | Template for environment variables, none needed yet |

## Related work

&#91;Four KTH ID2223 water-level projects and the difference to FlowGuard. Write this late, see the review response E1.\]
