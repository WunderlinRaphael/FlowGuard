"""Compare the model ladder on the validation segment and log every run to MLflow.

    uv run --group train python -m flowguard.training.train --store <path or gs://...>

The test segment is not read or scored here; it is scored once at the end of MS3.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import mlflow
import pandas as pd

from flowguard.common.pipeline_config import load_pipeline_config
from flowguard.common.raw_store import default_store_path
from flowguard.common.stations import load_stations
from flowguard.training import ladder
from flowguard.training.data import LEVEL, WEATHER, build_datasets, read_feature_table

log = logging.getLogger(__name__)

EXPERIMENT = "flowguard-ladder"
REPO_ROOT = Path(__file__).resolve().parents[3]


def default_tracking_uri():
    # A local folder, git-ignored. Absolute, so the result does not depend on the working folder.
    return (REPO_ROOT / "mlruns").as_uri()


def git_info(root=REPO_ROOT):
    """(commit, dirty). Commit from the environment (CI), else read from git, else "unknown".

    Only reads, never changes the repository.
    """
    commit = os.environ.get("GIT_COMMIT") or os.environ.get("GITHUB_SHA")
    dirty = None
    try:
        if commit is None:
            commit = _git(root, "rev-parse", "HEAD")
        dirty = bool(_git(root, "status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError):
        pass
    return commit or "unknown", dirty


def _git(root, *args):
    out = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True, timeout=30
    )
    return out.stdout.strip()


def select_variants(features):
    """Persistence is always in; level and weather select the others."""
    return [
        v
        for v in ladder.VARIANTS
        if v.feature_set is None or features == "all" or v.feature_set == features
    ]


def run_ladder(table, config, stations, variants, seed):
    """Fit every variant. Returns {variant name: (Variant, Datasets, Fit)}."""
    results = {}
    for variant in variants:
        # Persistence needs no inputs, only the validation rows (the same for every variant).
        feature_set = variant.feature_set or LEVEL
        datasets = build_datasets(
            table, config, stations, feature_set, weather_era_only=variant.weather_era_only
        )
        if len(datasets.val.y) == 0 or len(datasets.train.y) == 0:
            raise ValueError(f"No train or validation rows for {variant.name}")
        log.info("fitting %s", variant.name)
        results[variant.name] = (variant, datasets, ladder.run_variant(variant, datasets, seed))
    return results


def log_run(variant, datasets, fit, scores, manifest, config, seed, git):
    """One MLflow run per variant: params, metrics per station, tags for provenance."""
    commit, dirty = git
    params = {"feature_set": variant.feature_set or "none", "seed": seed}
    params["weather_era_only"] = variant.weather_era_only
    params["train_rows"] = len(datasets.train.y)
    params["val_rows"] = len(datasets.val.y)
    for name, data in (("train", datasets.train), ("val", datasets.val)):
        params[f"{name}_first_t"] = data.t.min().isoformat()
        params[f"{name}_last_t"] = data.t.max().isoformat()
    if variant.model == "ridge":
        params.update({f"ridge_{k}": v for k, v in ladder.RIDGE_PARAMS.items()})
    if variant.model == "lightgbm":
        params.update({f"lgbm_{k}": v for k, v in ladder.LIGHTGBM_PARAMS.items()})
        params["lgbm_early_stopping_rounds"] = ladder.EARLY_STOPPING_ROUNDS

    with mlflow.start_run(run_name=variant.name):
        mlflow.log_params(params)
        mlflow.set_tags(
            {
                "model": variant.model,
                "git_commit": commit,
                "git_dirty": str(dirty),
                "feature_version": manifest["feature_version"],
                "feature_built_at": manifest["built_at"],
                "feature_code_version": manifest["code_version"],
                "feature_newest_fetched_at": json.dumps(manifest["newest_fetched_at"]),
            }
        )
        # The split dates and the model parameters, as one file next to the run.
        mlflow.log_dict(
            {
                "split": {k: str(v) for k, v in asdict(config.split).items()},
                "first_weather_origin": config.first_weather_origin.isoformat(),
                "ridge": ladder.RIDGE_PARAMS,
                "lightgbm": ladder.LIGHTGBM_PARAMS,
            },
            "config.json",
        )
        for _, row in scores.iterrows():
            mlflow.log_metric(f"mae_cm_{row['station']}", row["mae_cm"])
            mlflow.log_metric(f"ratio_{row['station']}", row["ratio"])
        mlflow.log_metric("fit_seconds", fit.seconds)
        if fit.best_iteration is not None:
            mlflow.log_metric("best_iteration", fit.best_iteration)


def run(store, features, seed, tracking_uri, config=None, stations=None, git=None):
    config = config or load_pipeline_config()
    stations = stations or load_stations()
    table, manifest = read_feature_table(store, config, stations)
    results = run_ladder(table, config, stations, select_variants(features), seed)

    # Persistence defines the ratio, and it is scored on the validation rows all variants share.
    fits = {name: fit for name, (_, _, fit) in results.items()}
    val = next(iter(results.values()))[1].val
    scores = ladder.score_variants(fits, val)

    # MLflow 3 refuses the local file store unless this is set. The file store is kept on
    # purpose for this step (a plain mlruns/ folder); the registry step can move to SQLite.
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(EXPERIMENT)
    git = git or git_info()
    for name, (variant, datasets, fit) in results.items():
        log_run(
            variant,
            datasets,
            fit,
            scores[scores["variant"] == name],
            manifest,
            config,
            seed,
            git,
        )
    return scores


def format_scores(scores):
    table = scores.copy()
    table["mae_cm"] = table["mae_cm"].map("{:.2f}".format)
    table["ratio"] = table["ratio"].map("{:.2f}".format)
    table["fit_seconds"] = table["fit_seconds"].map("{:.1f}".format)
    table["best_iteration"] = table["best_iteration"].map(
        lambda v: "" if pd.isna(v) else f"{int(v)}"
    )
    return table.to_string(index=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", default=None, help="local folder or gs:// path")
    parser.add_argument("--features", choices=["all", LEVEL, WEATHER], default="all")
    parser.add_argument("--seed", type=int, default=ladder.DEFAULT_SEED)
    parser.add_argument("--tracking-uri", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    scores = run(
        args.store or default_store_path(),
        args.features,
        args.seed,
        args.tracking_uri or default_tracking_uri(),
    )
    print(format_scores(scores))
    return 0


if __name__ == "__main__":
    sys.exit(main())
