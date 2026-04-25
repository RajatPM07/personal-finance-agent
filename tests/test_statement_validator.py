from datetime import date
from decimal import Decimal

from skills.finance.ingestion._common import ParsedRow, ParseResult


def _make_pr(rows, total_spends, total_credits):
    return ParseResult(
        rows=rows,
        declared_totals={
            "total_spends": Decimal(str(total_spends)),
            "total_credits": Decimal(str(total_credits)),
            "closing_balance": None,
        },
        pdf_content_hash="abc",
        parser_version="test/v1",
    )


def _row(amount, direction, ordinal=1):
    return ParsedRow(
        txn_date=date(2026, 5, 15),
        amount=Decimal(str(amount)),
        direction=direction,
        raw_merchant="test",
        source_row_ordinal=ordinal,
    )


def test_validator_exact_match():
    from skills.finance.ingestion.statement_validator import validate
    pr = _make_pr([_row(100, "out", 1), _row(50, "out", 2), _row(30, "in", 3)], 150, 30)
    res = validate(pr)
    assert res.ok is True
    assert res.delta_out == Decimal("0")
    assert res.delta_in == Decimal("0")


def test_validator_within_tolerance_rs1():
    from skills.finance.ingestion.statement_validator import validate
    pr = _make_pr([_row(100, "out", 1), _row(50, "out", 2)], "150.50", 0)
    res = validate(pr)
    assert res.ok is True
    assert res.delta_out == Decimal("0.50")


def test_validator_over_tolerance_rejects():
    from skills.finance.ingestion.statement_validator import validate
    pr = _make_pr([_row(100, "out", 1), _row(50, "out", 2)], 152, 0)
    res = validate(pr)
    assert res.ok is False
    assert res.delta_out == Decimal("2")


def test_validator_all_credits():
    from skills.finance.ingestion.statement_validator import validate
    pr = _make_pr([_row(200, "in", 1), _row(50, "in", 2)], 0, 250)
    res = validate(pr)
    assert res.ok is True


def test_validator_zero_rows_zero_totals():
    from skills.finance.ingestion.statement_validator import validate
    pr = _make_pr([], 0, 0)
    res = validate(pr)
    assert res.ok is True
    assert res.rows_count == 0


def test_validator_signed_amount_negative_rejected():
    """Defensive: if a parser ever emits negative amount (it shouldn't per the
    ParsedRow contract), validator computes correctly via the sum logic.
    A negative-amount row will cause delta to exceed tolerance."""
    from skills.finance.ingestion.statement_validator import validate
    pr = _make_pr([_row(-100, "out", 1)], 100, 0)
    res = validate(pr)
    assert res.ok is False
