from decimal import Decimal

import pandas as pd
import pytest


def test_money_sent_to_with_own_handle_is_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details="Money sent to Rajat Sharma",
        other_transaction_details="UPI Ref ABC / 7358467199@ptsbi",
        own_handles=own,
    ) is True


def test_money_sent_to_other_handle_is_not_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details="Money sent to Md Mehboob",
        other_transaction_details="9123456789@upi",
        own_handles=own,
    ) is False


def test_paid_to_prefix_is_never_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    # Even if the row has the own-handle in the UPI ID column (theoretical),
    # 'Paid to ...' is a merchant payment, not a self-transfer.
    assert classify_self_transfer(
        transaction_details="Paid to Munni Sharma",
        other_transaction_details="7358467199@ptsbi",
        own_handles=own,
    ) is False


def test_received_from_is_never_self_transfer():
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details="Received from Aayushi Shukla",
        other_transaction_details="7358467199@ptsbi",
        own_handles=own,
    ) is False


def test_empty_own_handles_returns_false():
    """If we have no own-handles configured, no row is a self-transfer.
    (V1 fallback when the accounts table has no UPI-typed rows.)"""
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    assert classify_self_transfer(
        transaction_details="Money sent to Anyone",
        other_transaction_details="some@upi",
        own_handles=[],
    ) is False


def test_none_inputs_handled():
    """NaN/None values from pandas show up as None in some rows; classifier
    must tolerate them without crashing."""
    from skills.finance.ingestion.parsers.paytm_upi import classify_self_transfer
    own = ["7358467199@ptsbi"]
    assert classify_self_transfer(
        transaction_details=None,
        other_transaction_details=None,
        own_handles=own,
    ) is False


# _read_summary_totals -------------------------------------------------------

def _build_synthetic_summary() -> pd.DataFrame:
    """Mirror the structure observed in the real Paytm Summary sheet
    (verified during 2026-04-26 inspection): declared totals at rows 9-12,
    addressable by label-scan rather than fixed-row indexing."""
    rows = [[None] * 5 for _ in range(15)]
    rows[8] = ["Paytm Statement for :", "Apr 2025 - Mar 2026", None, None, None]
    rows[9] = ["Money Paid (Amount in Rs.)", "1,23,456.78", None, None, None]
    rows[10] = ["Money Paid (No. of Payments)", 698, None, None, None]
    rows[11] = ["Money Received (Amount in Rs.)", "5,000.00", None, None, None]
    rows[12] = ["Money Received (No. of Payments)", 8, None, None, None]
    return pd.DataFrame(rows)


def test_read_summary_totals_extracts_paid_amount():
    from skills.finance.ingestion.parsers.paytm_upi import _read_summary_totals
    totals = _read_summary_totals(_build_synthetic_summary())
    assert totals["paid_amount"] == Decimal("123456.78")
    assert totals["paid_count"] == 698
    assert totals["recv_amount"] == Decimal("5000.00")
    assert totals["recv_count"] == 8


def test_read_summary_totals_handles_string_with_indian_separators():
    """Indian number format uses comma at thousand AND lakh boundaries
    (e.g. '1,23,456.78'). Parser must strip commas before Decimal."""
    from skills.finance.ingestion.parsers.paytm_upi import _read_summary_totals
    df = _build_synthetic_summary()
    df.iat[9, 1] = "12,34,567.89"
    totals = _read_summary_totals(df)
    assert totals["paid_amount"] == Decimal("1234567.89")


def test_read_summary_totals_raises_when_labels_missing():
    """If Paytm changes the Summary layout, fail loud rather than silently
    pass with zero totals."""
    from skills.finance.ingestion.parsers.paytm_upi import (
        ParserError,
        _read_summary_totals,
    )
    df = pd.DataFrame([["random", "data"], ["here", "no labels"]])
    with pytest.raises(ParserError) as exc_info:
        _read_summary_totals(df)
    assert "Money Paid" in str(exc_info.value) or "label" in str(exc_info.value).lower()


# _infer_direction + _strip_tag ---------------------------------------------

def test_infer_direction_paid_to():
    from skills.finance.ingestion.parsers.paytm_upi import _infer_direction
    assert _infer_direction("Paid to NETC FASTag Recharge") == "out"


def test_infer_direction_money_sent_to():
    from skills.finance.ingestion.parsers.paytm_upi import _infer_direction
    assert _infer_direction("Money sent to Md Mehboob") == "out"


def test_infer_direction_received_from():
    from skills.finance.ingestion.parsers.paytm_upi import _infer_direction
    assert _infer_direction("Received from Aayushi Shukla") == "in"


def test_infer_direction_unknown_prefix_raises():
    from skills.finance.ingestion.parsers.paytm_upi import (
        ParserError,
        _infer_direction,
    )
    with pytest.raises(ParserError) as exc_info:
        _infer_direction("Refund processed for order 123")
    assert "unknown" in str(exc_info.value).lower() or "prefix" in str(exc_info.value).lower()


def test_strip_paytm_tag_emoji():
    """Paytm tags like '#🥘 Food' should produce 'Food' as category_hint."""
    from skills.finance.ingestion.parsers.paytm_upi import _strip_tag
    assert _strip_tag("#🥘 Food") == "Food"
    assert _strip_tag("#🛒 Groceries") == "Groceries"
    assert _strip_tag("#⛽️ Fuel") == "Fuel"
    assert _strip_tag("#💵 Money Transfer") == "Money Transfer"
    assert _strip_tag("#🔄 Miscellaneous") == "Miscellaneous"


def test_strip_paytm_tag_blank_or_none_returns_none():
    from skills.finance.ingestion.parsers.paytm_upi import _strip_tag
    assert _strip_tag(None) is None
    assert _strip_tag("") is None
    assert _strip_tag("   ") is None
