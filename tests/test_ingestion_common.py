from unittest.mock import mock_open, patch

import pytest


def test_detect_bank_icici_canonical():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_cc_2026_05.pdf") == "icici_cc"


def test_detect_bank_icici_loose():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("Statement_April_2026_ICICI_CC.pdf") == "icici_cc"


def test_detect_bank_amex_loose_xlsx():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("AMEX_Statement_2026_05.xlsx") == "amex_cc"
    assert detect_bank_from_filename("american_express_may.xlsx") == "amex_cc"


def test_detect_bank_no_match_returns_none():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("randomfile.pdf") is None


def test_detect_bank_ambiguous_returns_none():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_amex_partnership_statement.pdf") is None


def test_password_lookup_unique_prefix():
    from skills.finance.ingestion._common import password_lookup
    fake_yaml = """
icici_cc_1008:
  pattern: "custom"
  value: "icici_pass_123"
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)):
        assert password_lookup("icici_cc") == "icici_pass_123"


def test_password_lookup_exact_key_with_last4():
    from skills.finance.ingestion._common import password_lookup
    fake_yaml = """
icici_cc_1008:
  value: "icici_pass_1008"
icici_cc_9999:
  value: "icici_pass_9999"
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)):
        assert password_lookup("icici_cc", last4="1008") == "icici_pass_1008"


def test_password_lookup_ambiguous_without_last4_raises():
    from skills.finance.ingestion._common import AmbiguousCredentialError, password_lookup
    fake_yaml = """
icici_cc_1008:
  value: "p1"
icici_cc_9999:
  value: "p2"
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)), \
         pytest.raises(AmbiguousCredentialError):
        password_lookup("icici_cc")
