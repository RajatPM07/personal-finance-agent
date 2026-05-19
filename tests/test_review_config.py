"""W4.1 sql_agent reviewer-layer threshold loader. Yaml-driven so tuning
during calibration does not require a code commit."""
from __future__ import annotations

import textwrap

import pytest

from skills.finance.agents.review_config import ReviewConfig, load_review_config


def test_load_defaults_from_committed_yaml():
    cfg = load_review_config()
    assert isinstance(cfg, ReviewConfig)
    assert cfg.confidence_threshold == 0.85
    assert cfg.max_retry_rounds == 2
    assert cfg.anthropic_balance_warning_usd == 3.0


def test_load_from_custom_path(tmp_path):
    p = tmp_path / "custom.yaml"
    p.write_text(textwrap.dedent("""\
        confidence_threshold: 0.5
        max_retry_rounds: 1
        anthropic_balance_warning_usd: 1.0
    """))
    cfg = load_review_config(p)
    assert cfg.confidence_threshold == 0.5
    assert cfg.max_retry_rounds == 1


def test_unknown_key_raises(tmp_path):
    """Extra keys = silent config drift. Refuse rather than ignore."""
    p = tmp_path / "bad.yaml"
    p.write_text(textwrap.dedent("""\
        confidence_threshold: 0.85
        max_retry_rounds: 2
        anthropic_balance_warning_usd: 3.0
        mystery_setting: true
    """))
    with pytest.raises(ValueError, match="unknown.*mystery_setting"):
        load_review_config(p)


def test_missing_required_key_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("confidence_threshold: 0.85\n")
    with pytest.raises(ValueError, match="missing.*max_retry_rounds"):
        load_review_config(p)


def test_confidence_threshold_out_of_range_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(textwrap.dedent("""\
        confidence_threshold: 1.5
        max_retry_rounds: 2
        anthropic_balance_warning_usd: 3.0
    """))
    with pytest.raises(ValueError, match="confidence_threshold"):
        load_review_config(p)
