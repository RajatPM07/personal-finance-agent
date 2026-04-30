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


def test_bank_literal_includes_icici_savings():
    """ICICI Savings added in W3.4 — type-level guard against typos."""
    from typing import get_args

    from skills.finance.ingestion._common import Bank
    assert "icici_savings" in get_args(Bank)
    assert "icici_cc" in get_args(Bank)
    assert "amex_cc" in get_args(Bank)
    assert "paytm_upi" in get_args(Bank)


def test_parsed_row_accepts_icici_savings_fields():
    """is_upi_skip and txn_mode default safely so other parsers' rows
    construct unchanged."""
    from datetime import date
    from decimal import Decimal

    from skills.finance.ingestion._common import ParsedRow

    r = ParsedRow(
        txn_date=date(2026, 4, 30),
        amount=Decimal("100"),
        direction="out",
        raw_merchant="Test",
        source_row_ordinal=1,
    )
    assert r.is_upi_skip is False
    assert r.txn_mode is None

    r2 = ParsedRow(
        txn_date=date(2026, 4, 30),
        amount=Decimal("500"),
        direction="out",
        raw_merchant="UPI/SOMEONE/12345",
        source_row_ordinal=2,
        is_upi_skip=True,
        txn_mode="UPI",
    )
    assert r2.is_upi_skip is True
    assert r2.txn_mode == "UPI"


def test_insertable_rows_excludes_is_upi_skip():
    """ParseResult.insertable_rows() filters out is_upi_skip=True rows
    (in addition to the existing is_amex_routed filter from W3.1)."""
    from datetime import date
    from decimal import Decimal

    from skills.finance.ingestion._common import ParsedRow, ParseResult

    keep_neft = ParsedRow(
        txn_date=date(2026, 4, 30), amount=Decimal("50000"), direction="in",
        raw_merchant="NEFT-LOMBARD", source_row_ordinal=1,
        is_upi_skip=False, txn_mode="NEFT",
    )
    drop_upi = ParsedRow(
        txn_date=date(2026, 4, 30), amount=Decimal("500"), direction="out",
        raw_merchant="UPI/SOMEONE", source_row_ordinal=2,
        is_upi_skip=True, txn_mode="UPI",
    )

    pr = ParseResult(
        rows=[keep_neft, drop_upi],
        declared_totals={"total_spends": Decimal("500"), "total_credits": Decimal("50000"),
                         "closing_balance": Decimal("100000"), "_derived_from_rows": False},
        pdf_content_hash="0" * 64,
        parser_version="test/v1",
    )
    insertable = pr.insertable_rows()
    assert len(insertable) == 1
    assert insertable[0].txn_mode == "NEFT"


def test_decimal_from_indian_str_in_common():
    """Helper moved from parsers/paytm_upi.py to _common for sharing.
    Behavior must remain identical."""
    from decimal import Decimal

    from skills.finance.ingestion._common import _decimal_from_indian_str

    assert _decimal_from_indian_str("1,23,456.78") == Decimal("123456.78")
    assert _decimal_from_indian_str(Decimal("100")) == Decimal("100")
    assert _decimal_from_indian_str("-1,234.56") == Decimal("-1234.56")
    assert _decimal_from_indian_str("+5,000.00") == Decimal("5000.00")


def test_detect_bank_paytm_canonical():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("paytm_upi_apr25_mar26.xlsx") == "paytm_upi"


def test_detect_bank_paytm_loose():
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("Paytm_UPI_Statement_2026.xlsx") == "paytm_upi"
    assert detect_bank_from_filename("my_paytm_export.xlsx") == "paytm_upi"


def test_detect_bank_paytm_does_not_break_existing():
    """Regression: ICICI + AMEX detection unchanged."""
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("icici_cc_2026_04.pdf") == "icici_cc"
    assert detect_bank_from_filename("amex_cc_2026_04.xlsx") == "amex_cc"


def test_detect_bank_ambiguous_paytm_plus_icici_returns_none():
    """If a filename mentions both Paytm and ICICI it's ambiguous."""
    from skills.finance.ingestion._common import detect_bank_from_filename
    assert detect_bank_from_filename("paytm_icici_combined.xlsx") is None


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
