"""Persistence on the real feature table must match docs/eda_notes.md.

Skipped when the local feature files are missing (they are not in git, so CI skips it).
"""

from pathlib import Path

import pytest

from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.stations import load_stations
from flowguard.training import ladder
from flowguard.training.data import LEVEL, build_datasets, read_feature_table

STORE = Path(__file__).resolve().parents[1] / "data"
# Validation MAE of persistence in cm, from docs/eda_notes.md.
EDA = {"2087": 4.04, "2491": 2.70, "2056": 9.77}


@pytest.mark.skipif(not (STORE / "features").exists(), reason="no local feature table")
def test_persistence_reproduces_the_eda_validation_mae():
    config, stations = load_pipeline_config(), load_stations()
    table, _ = read_feature_table(STORE, config, stations)
    val = build_datasets(table, config, stations, LEVEL).val
    mae = ladder.score(val, [0.0] * len(val.y))["mae_cm"]
    for station, expected in EDA.items():
        assert mae[station] == pytest.approx(expected, abs=0.01)
