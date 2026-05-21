"""W5.1 self-transfer pattern loader + matching util.

Per docs/superpowers/specs/2026-05-21-refund-detection-design.md §7:
config errors fail loud at module load time — better to crash detection
than process every row as not-a-self-transfer.
"""
from __future__ import annotations

import textwrap
from uuid import UUID

import pytest
import yaml

from skills.finance.categorization.refund_detector import (
    _load_patterns,
    _matches_self_transfer,
)

ICICI_CC = UUID("10000000-0000-0000-0000-000000000003")
AMEX_CC = UUID("10000000-0000-0000-0000-000000000005")


def test_load_patterns_from_committed_yaml():
    """Committed defaults: ICICI CC + AMEX CC patterns load with the documented
    marker substrings. Regression-guard against accidental config changes."""
    patterns = _load_patterns()
    assert ICICI_CC in patterns
    assert AMEX_CC in patterns
    assert "BBPS Payment received" in patterns[ICICI_CC]
    assert "PAYMENT RECEIVED. THANK YOU" in patterns[AMEX_CC]


def test_load_missing_file_raises(tmp_path):
    """Per §7: fail loud at load time, not silent partial behavior."""
    bogus = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError):
        _load_patterns(bogus)


def test_load_malformed_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("this is not: valid: yaml: at all: :::")
    with pytest.raises(yaml.YAMLError):
        _load_patterns(p)


def test_load_empty_patterns_list_raises(tmp_path):
    """An account with empty patterns list = config bug. Refuse silent
    'always returns False' behavior."""
    p = tmp_path / "empty.yaml"
    p.write_text(textwrap.dedent("""
        "10000000-0000-0000-0000-000000000003": []
    """))
    with pytest.raises(ValueError, match="empty"):
        _load_patterns(p)


def test_load_empty_pattern_string_raises(tmp_path):
    """An empty-string pattern would match every row. Refuse."""
    p = tmp_path / "empty_str.yaml"
    p.write_text(textwrap.dedent("""
        "10000000-0000-0000-0000-000000000003":
          - ""
    """))
    with pytest.raises(ValueError, match="empty"):
        _load_patterns(p)


def test_matches_self_transfer_case_insensitive():
    """Substring match, case-insensitive, multi-pattern OR."""
    patterns = ["BBPS Payment received", "PAYMENT RECEIVED. THANK YOU"]
    assert _matches_self_transfer("11373135294 BBPS Payment received 0", patterns) is True
    assert _matches_self_transfer("11373135294 bbps payment received 0", patterns) is True
    assert _matches_self_transfer("payment received. thank you", patterns) is True
    assert _matches_self_transfer("PaYmEnT REcEiVeD. ThAnK YoU", patterns) is True
    assert _matches_self_transfer("Some random merchant", patterns) is False
    assert _matches_self_transfer("", patterns) is False
    assert _matches_self_transfer(None, patterns) is False
