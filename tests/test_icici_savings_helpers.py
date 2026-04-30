def test_savings_parser_version():
    from skills.finance.ingestion.parsers.icici_savings import __parser_version__
    assert __parser_version__ == "icici-savings-pdf/v1"


def test_classify_upi_skip_true_for_mode_upi():
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="UPI", particulars="some text") is True


def test_classify_upi_skip_true_for_mode_lowercase_upi():
    """Defensive: MODE column may extract with mixed case."""
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="upi", particulars="some text") is True


def test_classify_upi_skip_true_for_particulars_upi_prefix():
    """OR-logic: when MODE extraction is finicky, PARTICULARS prefix
    catches the same row."""
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="OTHER", particulars="UPI/SOMEONE/12345/...") is True


def test_classify_upi_skip_false_for_neft():
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="NEFT", particulars="NEFT-LOMBARD-PAYROLL") is False


def test_classify_upi_skip_false_for_atm():
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode="ATM", particulars="ATM/CASH WDL") is False


def test_classify_upi_skip_handles_none():
    """Pandas NaN / None values must not crash."""
    from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip
    assert classify_upi_skip(mode=None, particulars=None) is False
    assert classify_upi_skip(mode="", particulars="") is False


def test_parse_savings_date_dd_mm_yyyy():
    from datetime import date

    from skills.finance.ingestion.parsers.icici_savings import _parse_savings_date
    assert _parse_savings_date("28-02-2026") == date(2026, 2, 28)


def test_parse_savings_date_other_formats():
    """Defensive: ICICI uses DD-MM-YYYY but tolerate ISO + slashed forms."""
    from datetime import date

    from skills.finance.ingestion.parsers.icici_savings import _parse_savings_date
    assert _parse_savings_date("2026-02-28") == date(2026, 2, 28)
    assert _parse_savings_date("28/02/2026") == date(2026, 2, 28)


def test_parse_savings_date_unparseable_raises():
    import pytest

    from skills.finance.ingestion.parsers.icici_savings import (
        ParserError,
        _parse_savings_date,
    )
    with pytest.raises(ParserError):
        _parse_savings_date("not a date")


def test_extract_data_row_full_columns():
    """A complete data line: DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE.
    All columns extracted; one of DEPOSITS/WITHDRAWALS is non-zero."""
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "28-02-2026 NEFT NEFT-LOMBARD-PAYROLL FEB 2026 1,86,062.00 0.00 1,98,407.67"
    row = _extract_data_row(line)
    assert row is not None
    assert row.txn_date.isoformat() == "2026-02-28"
    assert row.direction == "in"
    assert str(row.amount) == "186062.00"
    assert row.txn_mode == "NEFT"
    assert "LOMBARD-PAYROLL" in row.raw_merchant


def test_extract_data_row_withdrawal():
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "27-02-2026 ATM ATM-CASH WDL/ATMID/12345 0.00 5,000.00 1,93,407.67"
    row = _extract_data_row(line)
    assert row is not None
    assert row.direction == "out"
    assert str(row.amount) == "5000.00"
    assert row.txn_mode == "ATM"


def test_extract_data_row_upi_flagged_skip():
    """UPI row gets is_upi_skip=True and txn_mode='UPI'."""
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "26-02-2026 UPI UPI/SOMEONE/UPI-REF-12345/SBI 0.00 500.00 1,98,407.67"
    row = _extract_data_row(line)
    assert row is not None
    assert row.is_upi_skip is True
    assert row.txn_mode == "UPI"


def test_extract_data_row_no_date_returns_none():
    """Lines that don't start with a date (continuation lines, headers,
    page footers) → None. Caller treats those separately."""
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    assert _extract_data_row("DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE") is None
    assert _extract_data_row("Total: 46,400.00 2,43,672.08 17,715.85") is None
    assert _extract_data_row("continuation of description") is None
    assert _extract_data_row("") is None


def test_extract_data_row_zero_amount_both_columns_returns_none():
    """Defensive: if both deposits and withdrawals are 0, the row has no
    monetary content — skip (parallel to ICICI CC's `amt <= 0` filter)."""
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "28-02-2026 INT INT.PD-INFO 0.00 0.00 1,98,407.67"
    assert _extract_data_row(line) is None


def test_assemble_rows_single_line_each():
    """No continuation: each row is one line, ordinals are 1..N."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "28-02-2026 NEFT NEFT-LOMBARD-PAYROLL 1,86,062.00 0.00 1,98,407.67",
        "27-02-2026 ATM ATM-CASH WDL 0.00 5,000.00 1,93,407.67",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 2
    assert rows[0].source_row_ordinal == 1
    assert rows[1].source_row_ordinal == 2
    assert rows[0].txn_mode == "NEFT"
    assert rows[1].txn_mode == "ATM"


def test_assemble_rows_single_continuation_attaches_as_head_of_next():
    """The line above a date row is the HEAD of that date row's txn (it's a
    PARTICULARS wrap from the visible PDF row above). Between two date rows,
    the LAST continuation line is the head of the NEXT txn — not the tail of
    the previous. Validated against real fixtures: ICICI Savings PDFs render
    each txn as a 1-3 line block with the wrapped PARTICULARS prefix on the
    line above the date baseline."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "28-02-2026 NEFT NEFT-LOMBARD-PAYROLL 1,86,062.00 0.00 1,98,407.67",
        "REFERENCE-NUMBER-EXTRA-INFO",
        "27-02-2026 ATM ATM-CASH WDL 0.00 5,000.00 1,93,407.67",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 2
    # head-of-next: continuation prepends to the SECOND txn's raw_merchant
    assert rows[1].raw_merchant.startswith("REFERENCE-NUMBER-EXTRA-INFO")
    assert "ATM ATM-CASH WDL" in rows[1].raw_merchant
    # first txn unchanged (no tail attached when only one continuation existed)
    assert "REFERENCE-NUMBER-EXTRA-INFO" not in rows[0].raw_merchant


def test_assemble_rows_split_tail_then_head_between_two_dates():
    """When TWO continuation lines appear between two date rows, the LAST
    one is the head of the next txn and the EARLIER one is the tail of the
    previous txn. Mirrors the actual ICICI layout where each txn is a
    [optional head] [date row] [optional tail] block — between two
    consecutive dates, the boundary lands at the last line."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "28-02-2026 NEFT NEFT-LOMBARD-PAYROLL 1,86,062.00 0.00 1,98,407.67",
        "TAIL-OF-FIRST-TXN",
        "HEAD-OF-SECOND-TXN",
        "27-02-2026 ATM ATM-CASH WDL 0.00 5,000.00 1,93,407.67",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 2
    assert "TAIL-OF-FIRST-TXN" in rows[0].raw_merchant
    assert "HEAD-OF-SECOND-TXN" not in rows[0].raw_merchant
    assert rows[1].raw_merchant.startswith("HEAD-OF-SECOND-TXN")
    assert "TAIL-OF-FIRST-TXN" not in rows[1].raw_merchant


def test_assemble_rows_trailing_continuation_attaches_to_last_row():
    """Continuations after the LAST date row — with no further date to anchor
    them as 'head of next' — flush onto the last emitted row's raw_merchant."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "28-02-2026 NEFT NEFT-LOMBARD-PAYROLL 1,86,062.00 0.00 1,98,407.67",
        "TAIL-AFTER-LAST",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 1
    assert "TAIL-AFTER-LAST" in rows[0].raw_merchant


def test_extract_data_row_two_numerics_balance_increase_is_deposit():
    """When the date line has only 2 trailing numerics (typical real-PDF
    case where ICICI's empty deposit/withdrawal column collapses), direction
    is inferred from balance delta: balance went up → 'in'."""
    from decimal import Decimal

    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "01-02-2026 BANK/117989491265/HDFccb8d2a7f2d54e61a835bef99bd8 23,200.00 2,38,187.93"
    row = _extract_data_row(line, prev_balance=Decimal("214987.93"))
    assert row is not None
    assert row.direction == "in"
    assert row.amount == Decimal("23200.00")
    assert str(row.txn_date) == "2026-02-01"


def test_extract_data_row_two_numerics_balance_decrease_is_withdrawal():
    """Balance went down → 'out'."""
    from decimal import Decimal

    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "01-02-2026 C/StandardC/639803364697 90,000.00 1,48,187.93"
    row = _extract_data_row(line, prev_balance=Decimal("238187.93"))
    assert row is not None
    assert row.direction == "out"
    assert row.amount == Decimal("90000.00")


def test_extract_data_row_two_numerics_no_prev_balance_defaults_to_out():
    """First-of-statement edge case: no prev_balance available → default to
    'out' (safer to under-count credits than over-count). State machine
    almost always seeds prev_balance from the B/F row before the first txn."""
    from decimal import Decimal

    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "01-02-2026 BIL/Personal Loan EMI 21,818.00 28,114.85"
    row = _extract_data_row(line, prev_balance=None)
    assert row is not None
    assert row.direction == "out"
    assert row.amount == Decimal("21818.00")


def test_extract_data_row_one_numeric_returns_none():
    """B/F (or C/F) row has only the carry-forward balance and no monetary
    transaction. Returns None; the state machine still uses the balance for
    prev_balance carry-forward."""
    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    assert _extract_data_row("01-02-2026 B/F 2,14,987.93") is None


def test_extract_data_row_with_head_continuation_seeds_upi_mode():
    """When the head continuation starts with 'UPI/...' the row gets MODE='UPI'
    and is_upi_skip=True even if the date line itself has no MODE token
    (typical real-PDF layout)."""
    from decimal import Decimal

    from skills.finance.ingestion.parsers.icici_savings import _extract_data_row
    line = "01-02-2026 BANK/117989491265/HDFccb 23,200.00 2,38,187.93"
    head = "UPI/ANSHUL KUM/thisisanshul09/UPI/HDFC"
    row = _extract_data_row(line, prev_balance=Decimal("214987.93"), head_continuation=head)
    assert row is not None
    assert row.txn_mode == "UPI"
    assert row.is_upi_skip is True
    assert row.raw_merchant.startswith("UPI/ANSHUL KUM")


def test_assemble_rows_skips_header_total_blank():
    """Non-data lines (header / Total / blank / unrelated nominee block)
    don't disturb assembly."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE",
        "",
        "28-02-2026 NEFT NEFT-LOMBARD 1,86,062.00 0.00 1,98,407.67",
        "Total: 1,86,062.00 0.00 1,98,407.67",
        "ACCOUNT TYPE ACCOUNT NUMBER MICR CODE IFS CODE NAME OF NOMINEE*",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 1
    assert rows[0].source_row_ordinal == 1


def test_assemble_rows_continuation_only_attaches_after_a_data_row():
    """Stray continuation lines BEFORE any data row are dropped (no anchor
    to attach to). Avoid silently losing real data without a row context."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "stray text before any row",
        "28-02-2026 NEFT NEFT-LOMBARD 1,86,062.00 0.00 1,98,407.67",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 1
    assert "stray" not in rows[0].raw_merchant


def test_parse_total_row_basic():
    from decimal import Decimal

    from skills.finance.ingestion.parsers.icici_savings import _parse_total_row
    res = _parse_total_row("Total: 46,400.00 2,43,672.08 17,715.85")
    assert res is not None
    deposits, withdrawals, balance = res
    assert deposits == Decimal("46400.00")
    assert withdrawals == Decimal("243672.08")
    assert balance == Decimal("17715.85")


def test_parse_total_row_with_leading_whitespace():
    from skills.finance.ingestion.parsers.icici_savings import _parse_total_row
    res = _parse_total_row("    Total:  1,45,061.00  49,348.00  1,13,428.85")
    assert res is not None


def test_parse_total_row_non_total_returns_none():
    from skills.finance.ingestion.parsers.icici_savings import _parse_total_row
    assert _parse_total_row("28-02-2026 NEFT something 100.00 0.00 200.00") is None
    assert _parse_total_row("DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE") is None
    assert _parse_total_row("") is None


def test_aggregate_totals_sums_across_pages():
    """Multiple Total: rows from different pages → summed."""
    from decimal import Decimal

    from skills.finance.ingestion.parsers.icici_savings import _aggregate_totals
    lines = [
        "28-02-2026 NEFT NEFT-LOMBARD 1,86,062.00 0.00 1,98,407.67",
        "Total: 1,86,062.00 0.00 1,98,407.67",
        "27-02-2026 ATM ATM-CASH 0.00 5,000.00 1,93,407.67",
        "Total: 0.00 5,000.00 1,93,407.67",
    ]
    totals = _aggregate_totals(lines)
    assert totals["total_credits"] == Decimal("186062.00")
    assert totals["total_spends"] == Decimal("5000.00")
    assert totals["closing_balance"] == Decimal("193407.67")
    assert totals["_derived_from_rows"] is False


def test_aggregate_totals_no_total_rows_raises():
    """Defensive: if NO Total: rows are found, fail loud rather than
    silently using 0. ICICI savings statements always have explicit
    subtotals; their absence indicates a layout change."""
    import pytest

    from skills.finance.ingestion.parsers.icici_savings import (
        ParserError,
        _aggregate_totals,
    )
    with pytest.raises(ParserError):
        _aggregate_totals([
            "28-02-2026 NEFT NEFT-LOMBARD 1,86,062.00 0.00 1,98,407.67",
        ])
