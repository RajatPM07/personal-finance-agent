"""Tunable threshold loader for the W4.1 reviewer layer.

Yaml lives at config/sql_agent_review.yaml so calibration-driven
threshold updates do not require a code commit. Unknown keys raise
to catch silent config drift; missing required keys raise so partial
edits during tuning don't fall back to incoherent defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "sql_agent_review.yaml"

_REQUIRED_KEYS = {"confidence_threshold", "max_retry_rounds", "anthropic_balance_warning_usd"}


@dataclass(frozen=True)
class ReviewConfig:
    confidence_threshold: float
    max_retry_rounds: int
    anthropic_balance_warning_usd: float


def load_review_config(path: Path | None = None) -> ReviewConfig:
    p = path if path is not None else _DEFAULT_PATH
    with open(p) as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a mapping, got {type(data).__name__}")

    missing = _REQUIRED_KEYS - data.keys()
    if missing:
        raise ValueError(f"{p}: missing required keys: {sorted(missing)}")

    extra = data.keys() - _REQUIRED_KEYS
    if extra:
        raise ValueError(f"{p}: unknown keys (refuse silent drift): {sorted(extra)}")

    ct = float(data["confidence_threshold"])
    if not 0.0 <= ct <= 1.0:
        raise ValueError(f"confidence_threshold must be in [0.0, 1.0]; got {ct}")

    mr = int(data["max_retry_rounds"])
    if mr < 0:
        raise ValueError(f"max_retry_rounds must be >= 0; got {mr}")

    bw = float(data["anthropic_balance_warning_usd"])
    if bw < 0:
        raise ValueError(f"anthropic_balance_warning_usd must be >= 0; got {bw}")

    return ReviewConfig(
        confidence_threshold=ct,
        max_retry_rounds=mr,
        anthropic_balance_warning_usd=bw,
    )
