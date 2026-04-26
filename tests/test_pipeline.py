from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID

from skills.finance.ingestion._common import ParsedRow, ParseResult, SourceMeta

ICICI_CC_ACCOUNT_ID = UUID("10000000-0000-0000-0000-000000000003")


def _make_pr(rows, total_spends, total_credits, parser_version="icici-cc/v1"):
    return ParseResult(
        rows=rows,
        declared_totals={
            "total_spends": Decimal(str(total_spends)),
            "total_credits": Decimal(str(total_credits)),
            "closing_balance": None,
        },
        pdf_content_hash="abc" * 21 + "z",
        parser_version=parser_version,
    )


def _row(amount, ordinal, direction="out", merchant="test"):
    return ParsedRow(
        txn_date=date(2026, 5, 15),
        amount=Decimal(str(amount)),
        direction=direction,
        raw_merchant=merchant,
        source_row_ordinal=ordinal,
    )


def _make_table_mock(txn_inserts, log_inserts):
    def mock_table(name):
        builder = MagicMock()
        if name == "transactions":
            def _upsert(rows, **kw):
                txn_inserts.append(rows)
                builder.execute = lambda: MagicMock(data=rows)
                return builder
            builder.upsert = _upsert
        elif name == "ingestion_log":
            def _insert(row):
                log_inserts.append(row)
                builder.execute = lambda: MagicMock(data=[row])
                return builder
            builder.insert = _insert
        return builder
    return mock_table


def test_pipeline_validation_failure_no_insert():
    """Totals mismatch → no transactions inserted, ingestion_log gets total_check_failed."""
    from skills.finance.ingestion.pipeline import ingest

    pr = _make_pr([_row(100, 1), _row(50, 2)], 200, 0)
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    txn_inserts, log_inserts = [], []
    fake_client = MagicMock()
    fake_client.table.side_effect = _make_table_mock(txn_inserts, log_inserts)

    with patch("skills.finance.ingestion.pipeline.service_client", return_value=fake_client):
        result = asyncio.run(ingest(pr, ICICI_CC_ACCOUNT_ID, source))

    assert len(txn_inserts) == 0
    assert len(log_inserts) == 1
    assert log_inserts[0]["status"] == "total_check_failed"
    assert result["status"] == "total_check_failed"


def test_pipeline_success_inserts_rows_and_logs():
    from skills.finance.ingestion.pipeline import ingest

    pr = _make_pr([_row(100, 1, merchant="SWIGGY"), _row(50, 2, merchant="BLINKIT")], 150, 0)
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    txn_inserts, log_inserts = [], []
    fake_client = MagicMock()
    fake_client.table.side_effect = _make_table_mock(txn_inserts, log_inserts)

    with patch("skills.finance.ingestion.pipeline.service_client", return_value=fake_client):
        result = asyncio.run(ingest(pr, ICICI_CC_ACCOUNT_ID, source))

    assert len(txn_inserts) == 1
    inserted = txn_inserts[0]
    assert len(inserted) == 2
    assert inserted[0]["raw_merchant"] == "SWIGGY"
    assert len(inserted[0]["import_hash"]) == 64
    assert inserted[0]["parser_version"] == "icici-cc/v1"
    assert result["status"] == "success"


def test_pipeline_import_hash_per_row_uses_mode_b():
    from skills.finance.ingestion.pipeline import ingest

    pr = _make_pr([_row(350, 1, merchant="SWIGGY"), _row(350, 2, merchant="SWIGGY")], 700, 0)
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    captured_hashes = []

    def fake_import_hash_pdf(**kwargs):
        captured_hashes.append(kwargs)
        return "h" * 64

    txn_inserts, log_inserts = [], []
    fake_client = MagicMock()
    fake_client.table.side_effect = _make_table_mock(txn_inserts, log_inserts)

    with patch("skills.finance.ingestion.pipeline.service_client", return_value=fake_client), \
         patch("skills.finance.ingestion.pipeline.import_hash_pdf",
               side_effect=fake_import_hash_pdf):
        asyncio.run(ingest(pr, ICICI_CC_ACCOUNT_ID, source))

    assert len(captured_hashes) == 2
    assert captured_hashes[0]["source_row_ordinal"] == 1
    assert captured_hashes[1]["source_row_ordinal"] == 2
    assert captured_hashes[0]["pdf_content_hash"] == captured_hashes[1]["pdf_content_hash"]
    assert captured_hashes[0]["parser_version"] == "icici-cc/v1"
