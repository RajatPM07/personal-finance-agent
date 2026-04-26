from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

FIXTURE = Path(__file__).parent / "golden_fixtures" / "amex_sample.xlsx"


def test_amex_parser_version():
    from skills.finance.ingestion.parsers.amex_cc import __parser_version__
    assert __parser_version__ == "amex-cc-xlsx/v1"


def test_find_header_row_canonical_layout():
    """Synthetic DF: known column set on row 0."""
    from skills.finance.ingestion.parsers.amex_cc import find_header_row
    df = pd.DataFrame([
        ["Date", "Description", "Amount"],
        ["15/05/2026", "SWIGGY", 350.00],
        ["20/05/2026", "BLINKIT", 1200.00],
    ])
    idx, mapping = find_header_row(df)
    assert idx == 0
    assert "date" in mapping and "description" in mapping and "amount" in mapping


def test_find_header_row_offset_layout():
    """AMEX exports often have preamble rows before the headers."""
    from skills.finance.ingestion.parsers.amex_cc import find_header_row
    df = pd.DataFrame([
        ["AMEX Statement", None, None],
        ["Customer: Rajat", None, None],
        ["", None, None],
        ["Date", "Description", "Amount"],
        ["15/05/2026", "SWIGGY", 350.00],
    ])
    idx, _ = find_header_row(df)
    assert idx == 3


def test_find_header_row_alternate_naming():
    from skills.finance.ingestion.parsers.amex_cc import find_header_row
    df = pd.DataFrame([
        ["Transaction Date", "Description of Transaction", "Amount"],
        ["15/05/2026", "SWIGGY", 350.00],
    ])
    idx, _ = find_header_row(df)
    assert idx == 0


def test_find_header_row_no_match_raises_with_preview():
    from skills.finance.ingestion.parsers.amex_cc import ParserError, find_header_row
    df = pd.DataFrame([
        ["Foo", "Bar", "Baz"],
        ["X", "Y", "Z"],
    ])
    with pytest.raises(ParserError) as exc_info:
        find_header_row(df)
    msg = str(exc_info.value).lower()
    assert "foo" in msg or "actual headers" in msg or "preview" in msg


def test_amex_amount_signed_convention():
    """AMEX exports with signed amounts: positive=charge (out), negative=credit (in)."""
    from skills.finance.ingestion.parsers.amex_cc import _row_from_signed_amount
    row = _row_from_signed_amount(
        date_str="11/22/2025", description="SWIGGY", amount_value=350.00, ordinal=1,
    )
    assert row.amount == Decimal("350.00")
    assert row.direction == "out"

    refund = _row_from_signed_amount(
        date_str="11/20/2025", description="REFUND ZOMATO", amount_value=-150.00, ordinal=2,
    )
    assert refund.amount == Decimal("150.00")
    assert refund.direction == "in"


@pytest.mark.skipif(not FIXTURE.exists(), reason="AMEX golden fixture not present")
def test_parse_real_amex_xlsx_returns_nonempty_parseresult():
    from skills.finance.ingestion.parsers.amex_cc import parse
    result = parse(FIXTURE)
    assert len(result.rows) > 0
    assert result.parser_version == "amex-cc-xlsx/v1"
    assert len(result.pdf_content_hash) == 64


@pytest.mark.skipif(not FIXTURE.exists(), reason="AMEX golden fixture not present")
def test_amex_parsed_row_fields_well_formed():
    from skills.finance.ingestion.parsers.amex_cc import parse
    result = parse(FIXTURE)
    for row in result.rows:
        assert row.amount > Decimal("0"), f"amount={row.amount} for ordinal={row.source_row_ordinal}"
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip()
        assert row.source_row_ordinal >= 1


@pytest.mark.skipif(not FIXTURE.exists(), reason="AMEX golden fixture not present")
def test_amex_ordinals_contiguous_1_to_n():
    from skills.finance.ingestion.parsers.amex_cc import parse
    result = parse(FIXTURE)
    ordinals = [r.source_row_ordinal for r in result.rows]
    assert ordinals == list(range(1, len(result.rows) + 1))


@pytest.mark.skipif(not FIXTURE.exists(), reason="AMEX golden fixture not present")
def test_amex_extracted_totals_pass_validator():
    """Whether declared totals come from a footer row or are derived from row sums,
    validator should pass on a real fixture."""
    from skills.finance.ingestion.parsers.amex_cc import parse
    from skills.finance.ingestion.statement_validator import validate
    result = parse(FIXTURE)
    val = validate(result)
    assert val.ok, (
        f"AMEX validator failed: delta_in={val.delta_in}, delta_out={val.delta_out}. "
        f"If a footer Total row exists, parser regex needs adjustment. "
        f"If absent, parser should fall back to row-sum-derived totals."
    )
