"""The model ladder: persistence, Ridge and pooled LightGBM, scored on validation.

Everything here works on the Datasets from data.py. The target is the 24 h change of W in cm,
so persistence predicts 0 and the error of every model is |prediction - change|.
"""

import statistics
import time
from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

DEFAULT_SEED = 42
FITS = 3  # fit time is the median of this many fits

RIDGE_PARAMS = {"alpha": 1.0}
# Fixed values, no search in this step. objective l1 because the target metric is the MAE.
LIGHTGBM_PARAMS = {
    "objective": "l1",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 50,
    "n_estimators": 2000,
}
EARLY_STOPPING_ROUNDS = 50


@dataclass(frozen=True)
class Variant:
    """One entry of the ladder: a model on a feature set and a training row rule."""

    model: str  # persistence, ridge or lightgbm
    feature_set: str | None  # level or weather; None for persistence
    weather_era_only: bool = False

    @property
    def name(self):
        if self.feature_set is None:
            return self.model
        return f"{self.model}_{self.feature_set}" + ("_era" if self.weather_era_only else "")


# (b) and (c) of LightGBM train on the same rows: the fair comparison with and without weather.
VARIANTS = (
    Variant("persistence", None),
    Variant("ridge", "level"),
    Variant("ridge", "weather"),
    Variant("lightgbm", "level"),
    Variant("lightgbm", "level", weather_era_only=True),
    Variant("lightgbm", "weather"),
)


@dataclass
class Fit:
    prediction: np.ndarray  # one value per validation row
    seconds: float  # median over FITS fits
    best_iteration: int | None = None
    extra: dict = field(default_factory=dict)


def timed_fits(fit, repeats=FITS, clock=time.perf_counter):
    """Run fit() several times. Returns the first result and the median duration in seconds.

    Every fit uses the same seed, so the repeats give the same model; only the time differs.
    """
    durations, first = [], None
    for _ in range(repeats):
        start = clock()
        result = fit()
        durations.append(clock() - start)
        if first is None:
            first = result
    return first, statistics.median(durations)


def fit_ridge(datasets, seed=DEFAULT_SEED):
    """One Ridge model per station. Missing values get the median of the training rows.

    Ridge has no randomness; seed is only there so all fit functions take the same arguments.
    """
    train, val = datasets.train, datasets.val
    prediction = np.full(len(val.y), np.nan)
    for station in sorted(val.station_id.unique()):
        train_rows = (train.station_id == station).to_numpy()
        val_rows = (val.station_id == station).to_numpy()
        # station_id is constant within a station, so Ridge does not use it. Columns that are
        # missing in every training row (e.g. the upstream series at Andermatt) are left out.
        rows = train.X[train_rows]
        columns = [c for c in rows.columns if c != "station_id" and rows[c].notna().any()]
        model = make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(), Ridge(**RIDGE_PARAMS)
        )
        model.fit(rows[columns], train.y[train_rows])
        prediction[val_rows] = model.predict(val.X.loc[val_rows, columns])
    return Fit(prediction=prediction, seconds=0.0)


def fit_lightgbm(datasets, seed=DEFAULT_SEED):
    """One pooled model for all stations; station_id is a categorical input.

    Early stopping looks at the validation segment, so the validation score is slightly
    optimistic. The test segment, scored once at the end, is not.
    """
    train, val = datasets.train, datasets.val
    model = lgb.LGBMRegressor(**LIGHTGBM_PARAMS, random_state=seed, verbose=-1)
    model.fit(
        train.X,
        train.y,
        eval_X=val.X,
        eval_y=val.y,
        eval_metric="l1",
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
    )
    return Fit(
        prediction=model.predict(val.X),
        seconds=0.0,
        best_iteration=int(model.best_iteration_),
    )


def run_variant(variant, datasets, seed=DEFAULT_SEED):
    """Fit the model of a variant (median of FITS fits) and return the Fit."""
    if variant.model == "persistence":
        return Fit(prediction=np.zeros(len(datasets.val.y)), seconds=0.0)
    fitter = {"ridge": fit_ridge, "lightgbm": fit_lightgbm}[variant.model]
    first, seconds = timed_fits(lambda: fitter(datasets, seed))
    first.seconds = seconds
    return first


def score(val, prediction):
    """MAE in cm per station: one row per station with the number of validation rows."""
    error = pd.Series(np.abs(np.asarray(prediction) - val.y.to_numpy()))
    by_station = error.groupby(val.station_id.to_numpy())
    return pd.DataFrame({"rows": by_station.size(), "mae_cm": by_station.mean()})


def score_variants(results, val):
    """Long table: variant x station with MAE, ratio to persistence, fit time, best iteration.

    results maps a variant name to its Fit and must contain "persistence".
    """
    persistence = score(val, results["persistence"].prediction)["mae_cm"]
    rows = []
    for name, fit in results.items():
        scored = score(val, fit.prediction)
        for station, row in scored.iterrows():
            rows.append(
                {
                    "variant": name,
                    "station": station,
                    "val_rows": int(row["rows"]),
                    "mae_cm": row["mae_cm"],
                    "ratio": row["mae_cm"] / persistence[station],
                    "fit_seconds": fit.seconds,
                    "best_iteration": fit.best_iteration,
                }
            )
    return pd.DataFrame(rows)
