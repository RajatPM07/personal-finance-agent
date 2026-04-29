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


def test_detect_bank_substring_cc_in_account_returns_none():
    """Regression: 'icici_account_2026.pdf' has 'cc' as substring of 'account'
    but does NOT have 'cc' as a standalone token. Must return None.
    Previously returned 'icici_cc' due to substring matching."""
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_account_2026.pdf") is None
    assert detect_bank_from_filename("icici_savings_account_2026_05.pdf") is None


def test_password_lookup_value_null_raises():
    """Regression: credentials.example.yaml ships with `paytm_statement: value: null`.
    If the user copies the example without filling values in, password_lookup
    must raise CredentialNotFoundError, not return None."""
    from skills.finance.ingestion._common import CredentialNotFoundError, password_lookup
    fake_yaml = """
icici_cc_1008:
  pattern: "custom"
  value: null
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)), \
         pytest.raises(CredentialNotFoundError):
        password_lookup("icici_cc")


def test_password_lookup_exact_key_missing_raises():
    """When last4 is supplied but no key matches, raise CredentialNotFoundError."""
    from skills.finance.ingestion._common import CredentialNotFoundError, password_lookup
    fake_yaml = """
other_cc_1008:
  value: "p"
"""
    with patch("builtins.open", mock_open(read_data=fake_yaml)), \
         pytest.raises(CredentialNotFoundError):
        password_lookup("icici_cc", last4="1008")


# W3.1 Paytm additions ----------------------------------------------------------

def test_bank_literal_includes_paytm_upi():
    """Paytm UPI added in W3.1 — type-level guard against typos."""
    from typing import get_args

    from skills.finance.ingestion._common import Bank
    assert "paytm_upi" in get_args(Bank)
    assert "icici_cc" in get_args(Bank)
    assert "amex_cc" in get_args(Bank)


def test_parsed_row_accepts_optional_paytm_fields():
    """New optional fields default safely so ICICI/AMEX construction is unchanged."""
    from datetime import date
    from decimal import Decimal

    from skills.finance.ingestion._common import ParsedRow

    # Minimal construction (existing parsers' usage) still works:
    r = ParsedRow(
        txn_date=date(2026, 4, 29),
        amount=Decimal("100"),
        direction="out",
        raw_merchant="Test",
        source_row_ordinal=1,
    )
    assert r.is_amex_routed is False
    assert r.is_self_transfer is False
    assert r.category_hint is None

    # Paytm-style construction sets the new fields:
    r2 = ParsedRow(
        txn_date=date(2026, 4, 29),
        amount=Decimal("500"),
        direction="out",
        raw_merchant="Some Merchant",
        source_row_ordinal=2,
        is_amex_routed=True,
        is_self_transfer=False,
        category_hint="Food",
    )
    assert r2.is_amex_routed is True
    assert r2.category_hint == "Food"


def test_parse_result_insertable_rows_excludes_amex_routed():
    from datetime import date
    from decimal import Decimal

    from skills.finance.ingestion._common import ParsedRow, ParseResult

    keep = ParsedRow(
        txn_date=date(2026, 4, 29), amount=Decimal("100"), direction="out",
        raw_merchant="Keep", source_row_ordinal=1,
    )
    drop = ParsedRow(
        txn_date=date(2026, 4, 29), amount=Decimal("200"), direction="out",
        raw_merchant="Drop", source_row_ordinal=2, is_amex_routed=True,
    )

    pr = ParseResult(
        rows=[keep, drop],
        declared_totals={"total_spends": Decimal("100"), "total_credits": Decimal("0"),
                         "closing_balance": None, "_derived_from_rows": False},
        pdf_content_hash="0" * 64,
        parser_version="test/v1",
    )
    insertable = pr.insertable_rows()
    assert len(insertable) == 1
    assert insertable[0].raw_merchant == "Keep"
