"""Calibration runner must space pairs out to stay under Groq free-tier RPM.

Without inter-pair sleep, a 20-pair run burns ~140 LLM calls in a tight loop
and crashes mid-batch on the harder query categories (multi-table joins,
time-window comparisons — the prompts that generate the most tokens). The
--pair-delay-seconds flag governs this; default must remain conservative
enough to keep the full 20-pair set completing on free Groq."""
from __future__ import annotations

from tests.sql_agent_calibration.run_calibration import _build_argparser


def test_default_pair_delay_is_3_seconds():
    """Regression-guard the default. If someone drops it below ~2s, the
    full 20-pair run will start crashing again on long-prompt categories."""
    args = _build_argparser().parse_args([])
    assert args.pair_delay_seconds == 3.0


def test_pair_delay_accepts_zero_for_tests():
    """Setting to 0 disables the sleep — used by unit tests + smoke runs
    against the example pairs (3 pairs, no rate-limit concern)."""
    args = _build_argparser().parse_args(["--pair-delay-seconds", "0"])
    assert args.pair_delay_seconds == 0.0


def test_pair_delay_accepts_float():
    args = _build_argparser().parse_args(["--pair-delay-seconds", "5.5"])
    assert args.pair_delay_seconds == 5.5
