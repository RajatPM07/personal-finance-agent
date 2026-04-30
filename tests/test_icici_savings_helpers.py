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
