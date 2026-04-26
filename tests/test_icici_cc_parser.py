from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "golden_fixtures" / "icici_sample.pdf"
PASSWORD = os.environ.get("ICICI_PDF_PASSWORD", "")


def test_parser_version_string():
    from skills.finance.ingestion.parsers.icici_cc import __parser_version__
    assert __parser_version__ == "icici-cc/v1"


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_parse_returns_nonempty_parseresult():
    from skills.finance.ingestion.parsers.icici_cc import parse
    result = parse(FIXTURE, password=PASSWORD)
    assert len(result.rows) > 0
    assert result.declared_totals["total_spends"] >= Decimal("0")
    assert result.declared_totals["total_credits"] >= Decimal("0")
    assert result.parser_version == "icici-cc/v1"
    assert len(result.pdf_content_hash) == 64


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_parsed_row_fields_well_formed():
    from skills.finance.ingestion.parsers.icici_cc import parse
    result = parse(FIXTURE, password=PASSWORD)
    for row in result.rows:
        assert row.amount > Decimal("0")
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip()
        assert row.source_row_ordinal >= 1


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_ordinals_contiguous_1_to_n():
    """CLAUDE.md testing §: assert ordinals contiguous 1..N — catches silent ordering drift."""
    from skills.finance.ingestion.parsers.icici_cc import parse
    result = parse(FIXTURE, password=PASSWORD)
    ordinals = [r.source_row_ordinal for r in result.rows]
    assert ordinals == list(range(1, len(result.rows) + 1))


@pytest.mark.skipif(not FIXTURE.exists(), reason="ICICI golden fixture not present")
@pytest.mark.skipif(not PASSWORD, reason="ICICI_PDF_PASSWORD env var not set")
def test_extracted_totals_match_declared_via_validator():
    """End-to-end: parser output -> validator -> ok=True for a real statement."""
    from skills.finance.ingestion.parsers.icici_cc import parse
    from skills.finance.ingestion.statement_validator import validate
    result = parse(FIXTURE, password=PASSWORD)
    val = validate(result)
    assert val.ok, (
        f"Validator failed on real ICICI fixture - "
        f"delta_in={val.delta_in}, delta_out={val.delta_out}. "
        f"Either parser regex is off, or declared totals labels need calibration."
    )
