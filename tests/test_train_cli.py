"""Tests for the training script: MLflow logging, git info, variant selection."""

import mlflow
import pytest
from training_helpers import make_config, make_table, write_store

from flowguard.common.stations import load_stations
from flowguard.training import ladder, train


@pytest.fixture(autouse=True)
def small_model(monkeypatch):
    monkeypatch.setitem(ladder.LIGHTGBM_PARAMS, "n_estimators", 60)


@pytest.fixture(scope="module")
def setup():
    config, stations = make_config(), load_stations()
    return config, stations, make_table(config, stations)


def runs(tracking_uri):
    mlflow.set_tracking_uri(tracking_uri)
    experiment = mlflow.get_experiment_by_name(train.EXPERIMENT)
    return mlflow.search_runs([experiment.experiment_id], output_format="list")


def test_run_logs_one_mlflow_run_per_variant(tmp_path, setup):
    config, stations, table = setup
    write_store(tmp_path / "store", table, config, stations)
    uri = (tmp_path / "mlruns").as_uri()
    scores = train.run(
        tmp_path / "store",
        "all",
        7,
        uri,
        config=config,
        stations=stations,
        git=("abc123", False),
    )
    assert set(scores["variant"]) == {v.name for v in ladder.VARIANTS}

    by_name = {r.data.tags["mlflow.runName"]: r for r in runs(uri)}
    assert set(by_name) == {v.name for v in ladder.VARIANTS}

    lgbm = by_name["lightgbm_weather"]
    assert lgbm.data.tags["git_commit"] == "abc123"
    assert lgbm.data.tags["git_dirty"] == "False"
    assert lgbm.data.tags["feature_version"] == config.feature_version
    assert lgbm.data.tags["feature_built_at"] == "2026-07-02T00:00:00Z"
    assert lgbm.data.params["seed"] == "7"
    assert lgbm.data.params["feature_set"] == "weather"
    assert lgbm.data.params["val_last_t"] < str(config.split.val_end.date())
    for station in ("2087", "2491", "2056"):
        assert f"mae_cm_{station}" in lgbm.data.metrics
        assert f"ratio_{station}" in lgbm.data.metrics
    assert lgbm.data.metrics["best_iteration"] >= 1
    assert "fit_seconds" in lgbm.data.metrics
    # Only LightGBM stops early; persistence has a ratio of exactly 1.
    assert "best_iteration" not in by_name["ridge_level"].data.metrics
    assert by_name["persistence"].data.metrics["ratio_2087"] == 1.0


def test_features_switch_selects_variants():
    names = lambda f: {v.name for v in train.select_variants(f)}
    assert names("level") == {"persistence", "ridge_level", "lightgbm_level", "lightgbm_level_era"}
    assert names("weather") == {"persistence", "ridge_weather", "lightgbm_weather"}
    assert names("all") == {v.name for v in ladder.VARIANTS}


def test_git_info_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_COMMIT", "deadbeef")
    commit, _ = train.git_info(tmp_path)
    assert commit == "deadbeef"


def test_git_info_without_git_is_unknown(monkeypatch, tmp_path):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    # tmp_path is not a repository, so git fails and the commit is unknown.
    assert train.git_info(tmp_path) == ("unknown", None)


def test_format_scores_prints_a_table(setup):
    import pandas as pd

    scores = pd.DataFrame(
        {
            "variant": ["persistence", "lightgbm_level"],
            "station": ["2087", "2087"],
            "val_rows": [10, 10],
            "mae_cm": [4.0, 3.6],
            "ratio": [1.0, 0.9],
            "fit_seconds": [0.0, 7.9],
            "best_iteration": [None, 246],
        }
    )
    text = train.format_scores(scores)
    assert "lightgbm_level" in text and "0.90" in text and "246" in text
