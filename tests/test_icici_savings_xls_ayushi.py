from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "golden_fixtures" / "icici_savings_xls_ayushi.xls"


def test_xls_parser_version():
    from skills.finance.ingestion.parsers.icici_savings_xls import __parser_version__
    assert __parser_version__ == "icici-savings-xls/v1"


@pytest.mark.skipif(not FIXTURE.exists(), reason="Ayushi ICICI Savings XLS fixture missing")
def test_parse_ayushi_xls_returns_nonempty_parseresult():
    from skills.finance.ingestion.parsers.icici_savings_xls import parse
    result = parse(FIXTURE)
    assert result.parser_version == "icici-savings-xls/v1"
    assert len(result.pdf_content_hash) == 64
    assert len(result.rows) > 0


@pytest.mark.skipif(not FIXTURE.exists(), reason="Ayushi ICICI Savings XLS fixture missing")
def test_ayushi_ordinals_contiguous_1_to_n():
    """CLAUDE.md test invariant — ordinals 1..N contiguous in result.rows."""
    from skills.finance.ingestion.parsers.icici_savings_xls import parse
    result = parse(FIXTURE)
    ordinals = [r.source_row_ordinal for r in result.rows]
    assert ordinals == list(range(1, len(result.rows) + 1))


@pytest.mark.skipif(not FIXTURE.exists(), reason="Ayushi ICICI Savings XLS fixture missing")
def test_ayushi_parsed_row_fields_well_formed():
    from skills.finance.ingestion.parsers.icici_savings_xls import parse
    result = parse(FIXTURE)
    for row in result.rows:
        assert row.amount > Decimal("0"), (
            f"non-positive amount at ordinal {row.source_row_ordinal}"
        )
        assert row.direction in ("in", "out"), (
            f"invalid direction at ordinal {row.source_row_ordinal}"
        )
        assert row.raw_merchant.strip(), (
            f"empty merchant at ordinal {row.source_row_ordinal}"
        )
        assert row.source_row_ordinal >= 1


@pytest.mark.skipif(not FIXTURE.exists(), reason="Ayushi ICICI Savings XLS fixture missing")
def test_ayushi_contains_upi_skip_rows():
    """Ayushi's savings are expected to contain UPI rows (PhonePe is source of truth)."""
    from skills.finance.ingestion.parsers.icici_savings_xls import parse
    result = parse(FIXTURE)
    upi_rows = [r for r in result.rows if r.is_upi_skip]
    assert len(upi_rows) > 0, "Expected at least some UPI rows in Ayushi's XLS fixture"
