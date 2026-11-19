"""Tests for the model ladder (synthetic data with a learnable signal)."""

import numpy as np
import pandas as pd
import pytest
from training_helpers import make_config, make_table

from flowguard.common.stations import load_stations
from flowguard.training import ladder
from flowguard.training.data import LEVEL, WEATHER, Dataset, Datasets, build_datasets


@pytest.fixture(scope="module")
def setup():
    config, stations = make_config(), load_stations()
    return config, stations, make_table(config, stations)


@pytest.fixture(autouse=True)
def small_model(monkeypatch):
    # Few rounds keep the tests fast; early stopping is still tested below.
    monkeypatch.setitem(ladder.LIGHTGBM_PARAMS, "n_estimators", 300)


def val_set(stations, y):
    return Dataset(
        X=pd.DataFrame(index=range(len(y))),
        y=pd.Series(y, dtype=float),
        station_id=pd.Series(stations),
        t=pd.Series(pd.date_range("2026-01-01", periods=len(y), freq="h", tz="UTC")),
    )


def test_persistence_mae_is_the_mean_absolute_change():
    val = val_set(["a", "a", "a", "b", "b"], [1.0, -3.0, 2.0, 10.0, -4.0])
    scored = ladder.score(val, np.zeros(5))
    assert scored.loc["a", "mae_cm"] == pytest.approx(2.0)
    assert scored.loc["b", "mae_cm"] == pytest.approx(7.0)
    assert scored.loc["a", "rows"] == 3


def test_ratio_is_model_mae_over_persistence_mae():
    val = val_set(["a", "a", "b", "b"], [2.0, -2.0, 4.0, 4.0])
    fits = {
        "persistence": ladder.Fit(np.zeros(4), 0.0),
        "model": ladder.Fit(np.array([1.0, -1.0, 4.0, 4.0]), 0.5),
    }
    table = ladder.score_variants(fits, val).set_index(["variant", "station"])
    assert table.loc[("persistence", "a"), "ratio"] == 1.0
    assert table.loc[("model", "a"), "ratio"] == pytest.approx(0.5)
    assert table.loc[("model", "b"), "ratio"] == pytest.approx(0.0)
    assert table.loc[("model", "a"), "fit_seconds"] == 0.5


def test_timed_fits_runs_three_fits_and_takes_the_median():
    calls = []
    ticks = iter([0.0, 1.0, 10.0, 13.0, 20.0, 22.0])  # durations 1, 3, 2

    def fit():
        calls.append(1)
        return len(calls)

    first, seconds = ladder.timed_fits(fit, clock=lambda: next(ticks))
    assert len(calls) == 3
    assert first == 1
    assert seconds == 2.0


@pytest.mark.parametrize("feature_set", [LEVEL, WEATHER])
def test_models_beat_persistence_on_a_learnable_signal(setup, feature_set):
    config, stations, table = setup
    data = build_datasets(table, config, stations, feature_set)
    results = {
        "persistence": ladder.run_variant(ladder.VARIANTS[0], data),
        "ridge": ladder.run_variant(ladder.Variant("ridge", feature_set), data),
        "lightgbm": ladder.run_variant(ladder.Variant("lightgbm", feature_set), data),
    }
    scores = ladder.score_variants(results, data.val)
    assert set(scores["station"]) == {"2087", "2491", "2056"}
    for name in ("ridge", "lightgbm"):
        assert (scores[scores["variant"] == name]["ratio"] < 0.5).all()
    assert len(results["ridge"].prediction) == len(data.val.y)
    assert not np.isnan(results["ridge"].prediction).any()


def test_lightgbm_stops_early_and_reports_the_iteration(setup):
    config, stations, table = setup
    data = build_datasets(table, config, stations, LEVEL)
    fit = ladder.run_variant(ladder.Variant("lightgbm", LEVEL), data)
    assert 1 <= fit.best_iteration <= ladder.LIGHTGBM_PARAMS["n_estimators"]
    assert fit.seconds > 0
    assert ladder.run_variant(ladder.VARIANTS[1], data).best_iteration is None


def test_same_seed_gives_the_same_prediction(setup):
    config, stations, table = setup
    data = build_datasets(table, config, stations, LEVEL)
    variant = ladder.Variant("lightgbm", LEVEL)
    a = ladder.run_variant(variant, data, seed=1)
    b = ladder.run_variant(variant, data, seed=1)
    np.testing.assert_allclose(a.prediction, b.prediction)


def test_ridge_leaves_out_columns_that_are_missing_in_training():
    # One station, one useful column and one column that is missing everywhere.
    n = 200
    rng = np.random.default_rng(0)
    x = rng.normal(size=n)
    X = pd.DataFrame({"a": x, "empty": np.nan, "station_id": pd.Categorical(["s"] * n)})
    data = Dataset(X, pd.Series(2 * x), pd.Series(["s"] * n), pd.Series(range(n)))
    fit = ladder.fit_ridge(Datasets(train=data, val=data))
    np.testing.assert_allclose(fit.prediction, 2 * x, atol=0.1)


def test_variants_cover_the_planned_ladder():
    names = [v.name for v in ladder.VARIANTS]
    assert names == [
        "persistence",
        "ridge_level",
        "ridge_weather",
        "lightgbm_level",
        "lightgbm_level_era",
        "lightgbm_weather",
    ]
