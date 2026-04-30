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


def test_assemble_rows_continuation_appends_to_previous():
    """Continuation line (no date) appends to the previous row's raw_merchant."""
    from skills.finance.ingestion.parsers.icici_savings import _assemble_rows
    lines = [
        "28-02-2026 NEFT NEFT-LOMBARD-PAYROLL 1,86,062.00 0.00 1,98,407.67",
        "REFERENCE-NUMBER-EXTRA-INFO",
        "27-02-2026 ATM ATM-CASH WDL 0.00 5,000.00 1,93,407.67",
    ]
    rows = _assemble_rows(lines)
    assert len(rows) == 2
    assert "REFERENCE-NUMBER-EXTRA-INFO" in rows[0].raw_merchant
    assert rows[0].raw_merchant.startswith("NEFT-LOMBARD-PAYROLL")


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
