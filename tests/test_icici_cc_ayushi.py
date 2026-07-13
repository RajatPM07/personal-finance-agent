from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

# Parametrize over two Ayushi card fixtures (gitignored, copied from source dir)
FIXTURE_AMAZONPAY = Path(__file__).parent / "golden_fixtures" / "icici_cc_ayushi_amazonpay.pdf"
FIXTURE_CARD2 = Path(__file__).parent / "golden_fixtures" / "icici_cc_ayushi_card2.pdf"
FIXTURES = [FIXTURE_AMAZONPAY, FIXTURE_CARD2]


class TestAyushiAmazonPayCard:
    """Verify ICICI CC parser on Ayushi's Amazon Pay card (13-* statements)."""

    @pytest.mark.skipif(
        not FIXTURE_AMAZONPAY.exists(), reason="Ayushi Amazon Pay fixture not present"
    )
    def test_parse_returns_nonempty_parseresult(self):
        from skills.finance.ingestion.parsers.icici_cc import parse

        result = parse(FIXTURE_AMAZONPAY, password="")
        assert len(result.rows) > 0
        assert result.parser_version == "icici-cc/v1"
        assert len(result.pdf_content_hash) == 64

    @pytest.mark.skipif(
        not FIXTURE_AMAZONPAY.exists(), reason="Ayushi Amazon Pay fixture not present"
    )
    def test_parsed_row_fields_well_formed(self):
        from skills.finance.ingestion.parsers.icici_cc import parse

        result = parse(FIXTURE_AMAZONPAY, password="")
        for row in result.rows:
            assert row.amount > Decimal("0")
            assert row.direction in ("in", "out")
            assert row.raw_merchant.strip()
            assert row.source_row_ordinal >= 1

    @pytest.mark.skipif(
        not FIXTURE_AMAZONPAY.exists(), reason="Ayushi Amazon Pay fixture not present"
    )
    def test_ordinals_contiguous_1_to_n(self):
        """Assert ordinals contiguous 1..N — catches silent ordering drift."""
        from skills.finance.ingestion.parsers.icici_cc import parse

        result = parse(FIXTURE_AMAZONPAY, password="")
        ordinals = [r.source_row_ordinal for r in result.rows]
        assert ordinals == list(range(1, len(result.rows) + 1))


class TestAyushiCard2:
    """Verify ICICI CC parser on Ayushi's 2nd card (17-* statements, includes credits)."""

    @pytest.mark.skipif(
        not FIXTURE_CARD2.exists(), reason="Ayushi Card2 fixture not present"
    )
    def test_parse_returns_nonempty_parseresult(self):
        from skills.finance.ingestion.parsers.icici_cc import parse

        result = parse(FIXTURE_CARD2, password="")
        assert len(result.rows) > 0
        assert result.parser_version == "icici-cc/v1"
        assert len(result.pdf_content_hash) == 64

    @pytest.mark.skipif(
        not FIXTURE_CARD2.exists(), reason="Ayushi Card2 fixture not present"
    )
    def test_parsed_row_fields_well_formed(self):
        from skills.finance.ingestion.parsers.icici_cc import parse

        result = parse(FIXTURE_CARD2, password="")
        for row in result.rows:
            assert row.amount > Decimal("0")
            assert row.direction in ("in", "out")
            assert row.raw_merchant.strip()
            assert row.source_row_ordinal >= 1

    @pytest.mark.skipif(
        not FIXTURE_CARD2.exists(), reason="Ayushi Card2 fixture not present"
    )
    def test_ordinals_contiguous_1_to_n(self):
        """Assert ordinals contiguous 1..N — catches silent ordering drift."""
        from skills.finance.ingestion.parsers.icici_cc import parse

        result = parse(FIXTURE_CARD2, password="")
        ordinals = [r.source_row_ordinal for r in result.rows]
        assert ordinals == list(range(1, len(result.rows) + 1))

    @pytest.mark.skipif(
        not FIXTURE_CARD2.exists(), reason="Ayushi Card2 fixture not present"
    )
    def test_contains_credit_payment_inbound(self):
        """Card2 contains INFINITY PAYMENT RECEIVED / BBPS credit; verify at least one inbound row."""
        from skills.finance.ingestion.parsers.icici_cc import parse

        result = parse(FIXTURE_CARD2, password="")
        inbound_rows = [r for r in result.rows if r.direction == "in"]
        assert (
            len(inbound_rows) > 0
        ), "Card2 statement should contain at least one inbound (credit) row"
