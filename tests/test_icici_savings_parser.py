from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

FIXTURE_FEB = Path(__file__).parent / "golden_fixtures" / "icici_savings_2026_02.pdf"
FIXTURE_JAN = Path(__file__).parent / "golden_fixtures" / "icici_savings_2026_01.pdf"


def _password() -> str:
    """Reuse the icici_cc password (per spec note: same credential value)."""
    from skills.finance.ingestion._common import password_lookup
    return password_lookup("icici_cc")


def test_savings_parser_version_via_parse_entrypoint():
    from skills.finance.ingestion.parsers.icici_savings import __parser_version__
    assert __parser_version__ == "icici-savings-pdf/v1"


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_parse_feb_returns_nonempty_parseresult():
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    assert result.parser_version == "icici-savings-pdf/v1"
    assert len(result.pdf_content_hash) == 64
    assert len(result.rows) > 30
    assert "total_spends" in result.declared_totals
    assert "total_credits" in result.declared_totals
    assert "closing_balance" in result.declared_totals


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_ordinals_contiguous_1_to_n():
    """CLAUDE.md test invariant — ordinals 1..N contiguous in result.rows."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    ordinals = [r.source_row_ordinal for r in result.rows]
    assert ordinals == list(range(1, len(result.rows) + 1))


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_validator_passes_feb():
    """Page-subtotal aggregation: extracted in/out matches declared in/out."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    from skills.finance.ingestion.statement_validator import validate
    result = parse(FIXTURE_FEB, _password())
    val = validate(result)
    assert val.ok, (
        f"Feb savings validator failed: delta_in={val.delta_in}, "
        f"delta_out={val.delta_out}. Declared: in={val.declared_in} out={val.declared_out}; "
        f"Extracted: in={val.extracted_in} out={val.extracted_out}."
    )


@pytest.mark.skipif(not FIXTURE_JAN.exists(), reason="ICICI Savings Jan fixture missing")
def test_savings_validator_passes_jan():
    """Same validator check for the Jan fixture (proves format stability)."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    from skills.finance.ingestion.statement_validator import validate
    result = parse(FIXTURE_JAN, _password())
    val = validate(result)
    assert val.ok, f"Jan delta_in={val.delta_in}, delta_out={val.delta_out}"


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_upi_rows_flagged_not_dropped():
    """D1: UPI rows appear in result.rows (validator-friendly) but NOT in
    result.insertable_rows() (insert-time skip)."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    upi_rows = [r for r in result.rows if r.is_upi_skip]
    assert len(upi_rows) > 0, "Expected at least some UPI rows in Feb fixture"
    insertable = result.insertable_rows()
    assert all(r not in insertable for r in upi_rows)
    assert len(insertable) == len(result.rows) - len(upi_rows)


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_txn_mode_populated_for_non_upi():
    """Every non-UPI row should have a non-NULL txn_mode (NEFT, IMPS, ATM, etc.)."""
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    non_upi = [r for r in result.rows if not r.is_upi_skip]
    assert len(non_upi) > 0
    for r in non_upi:
        assert r.txn_mode is not None and r.txn_mode != "", (
            f"non-UPI row at ordinal {r.source_row_ordinal} has empty txn_mode"
        )


@pytest.mark.skipif(not FIXTURE_FEB.exists(), reason="ICICI Savings Feb fixture missing")
def test_savings_parsed_row_fields_well_formed():
    from skills.finance.ingestion.parsers.icici_savings import parse
    result = parse(FIXTURE_FEB, _password())
    for row in result.rows:
        assert row.amount > Decimal("0"), (
            f"non-positive amount at ordinal {row.source_row_ordinal}"
        )
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip()
        assert row.source_row_ordinal >= 1
