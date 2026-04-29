from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "golden_fixtures" / "paytm_upi_apr25_mar26.xlsx"


def test_paytm_parser_version():
    from skills.finance.ingestion.parsers.paytm_upi import __parser_version__
    assert __parser_version__ == "paytm-upi-xlsx/v1"


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_parse_returns_nonempty_parseresult(monkeypatch):
    """Smoke: real fixture parses, ParseResult has expected shape."""
    from skills.finance.ingestion.parsers import paytm_upi
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    assert result.parser_version == "paytm-upi-xlsx/v1"
    assert len(result.pdf_content_hash) == 64
    assert len(result.rows) > 700      # expect ~711 (713 minus a few unparseable padding rows)
    assert "total_spends" in result.declared_totals
    assert "total_credits" in result.declared_totals


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_amex_routed_rows_flagged_not_dropped(monkeypatch):
    """D1: AMEX-routed rows are in result.rows but NOT in insertable_rows."""
    from skills.finance.ingestion.parsers import paytm_upi
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    amex_rows = [r for r in result.rows if r.is_amex_routed]
    assert len(amex_rows) == 2, "Summary said 2 AMEX-routed; parser must agree"
    insertable = result.insertable_rows()
    assert all(r not in insertable for r in amex_rows)
    assert len(insertable) == len(result.rows) - 2


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_ordinals_contiguous_1_to_n(monkeypatch):
    """CLAUDE.md test invariant: ordinals 1..N within result.rows."""
    from skills.finance.ingestion.parsers import paytm_upi
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    ordinals = [r.source_row_ordinal for r in result.rows]
    assert ordinals == list(range(1, len(result.rows) + 1))


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_validator_passes_on_real_fixture(monkeypatch):
    """The whole point of declared-totals adjustment: validator passes."""
    from skills.finance.ingestion.parsers import paytm_upi
    from skills.finance.ingestion.statement_validator import validate
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    val = validate(result)
    assert val.ok, (
        f"Paytm validator failed: delta_in={val.delta_in}, "
        f"delta_out={val.delta_out}. If delta_out roughly equals the "
        f"self-transfer total, the own-handles list may be incomplete."
    )


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_category_hint_populated(monkeypatch):
    """At least some rows should have category_hint set (Paytm tags most rows)."""
    from skills.finance.ingestion.parsers import paytm_upi
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    hinted = [r for r in result.rows if r.category_hint is not None]
    assert len(hinted) > 500, "Expected most rows to be tagged by Paytm"


@pytest.mark.skipif(not FIXTURE.exists(), reason="Paytm golden fixture not present")
def test_paytm_parsed_row_fields_well_formed(monkeypatch):
    from skills.finance.ingestion.parsers import paytm_upi
    monkeypatch.setattr(paytm_upi, "_load_own_upi_handles",
                        lambda: ["7358467199@ptsbi"])

    result = paytm_upi.parse(FIXTURE)
    for row in result.rows:
        assert row.amount > Decimal("0"), \
            f"non-positive amount at ordinal {row.source_row_ordinal}"
        assert row.direction in ("in", "out")
        assert row.raw_merchant.strip(), "empty merchant"
        assert row.source_row_ordinal >= 1
