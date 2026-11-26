"""Rules for scoring the risk bands, read from the `evaluation` section of config/pipeline.yaml."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class EvaluationConfig:
    # A band in a segment needs at least this many episodes to count toward the success criterion.
    min_episodes_per_band: int
    # Band hours more than this far apart belong to different episodes.
    episode_gap_hours: int
    # A thin band is pooled over all stations as one event set.
    pool_thin_bands: bool

    @property
    def episode_gap(self):
        return timedelta(hours=self.episode_gap_hours)


def parse_evaluation_config(raw):
    """Build an EvaluationConfig from the `evaluation` section and validate it."""
    config = EvaluationConfig(
        min_episodes_per_band=raw["min_episodes_per_band"],
        episode_gap_hours=raw["episode_gap_hours"],
        pool_thin_bands=raw["pool_thin_bands"],
    )
    validate_evaluation(config)
    return config


def validate_evaluation(config):
    """Raise ValueError if a setting has the wrong type or is out of range."""
    # bool is a subclass of int in Python, so a YAML `true` must be rejected explicitly.
    for name in ("min_episodes_per_band", "episode_gap_hours"):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"evaluation.{name} must be a whole number of at least 1")
    if config.pool_thin_bands is not True and config.pool_thin_bands is not False:
        raise ValueError("evaluation.pool_thin_bands must be true or false")
