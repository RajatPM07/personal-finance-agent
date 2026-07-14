from datetime import date
from decimal import Decimal
from uuid import UUID

from skills.finance.ingestion._common import ParsedRow, ParseResult, SourceMeta
from skills.finance.ingestion.pipeline import _build_insert_row


def _row():
    return ParsedRow(txn_date=date(2026, 1, 2), amount=Decimal("100.00"),
                     direction="out", raw_merchant="TEST", source_row_ordinal=1)


def _pr():
    return ParseResult(rows=[_row()], declared_totals={"total_spends": Decimal("100"),
                       "total_credits": Decimal("0"), "closing_balance": None,
                       "_derived_from_rows": True}, pdf_content_hash="a"*64,
                       parser_version="test/v1")


def test_build_insert_row_defaults_to_rajat():
    d = _build_insert_row(_row(), UUID(int=1), _pr(), SourceMeta("manual_pdf", "f.pdf"))
    assert d["user_id"] == "00000000-0000-0000-0000-000000000001"


def test_build_insert_row_honors_explicit_user():
    ayushi = "00000000-0000-0000-0000-000000000002"
    d = _build_insert_row(_row(), UUID(int=1), _pr(), SourceMeta("manual_pdf", "f.pdf"), user_id=ayushi)
    assert d["user_id"] == ayushi
