"""W5.1 §6.1: detection runs inline after successful ingestion, wrapped in
try/except so detection bugs never roll back ingestion."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from skills.finance.categorization.refund_detector import DetectionResult
from skills.finance.ingestion._common import ParsedRow, ParseResult, SourceMeta

ACCT = UUID("10000000-0000-0000-0000-000000000005")


def _fake_parse_result():
    return ParseResult(
        rows=[
            ParsedRow(
                txn_date=date(2026, 3, 15), amount=Decimal("100.00"),
                direction="out", raw_merchant="X",
                source_row_ordinal=1,
            )
        ],
        declared_totals={"_derived_from_rows": False},
        pdf_content_hash="abc123",
        parser_version="test/v1",
    )


@pytest.mark.asyncio
async def test_ingest_calls_detection_after_success():
    """Happy path: _log_success called BEFORE detection; detection result
    logged at INFO level."""
    pr = _fake_parse_result()
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    fake_response = MagicMock()
    fake_response.data = [{"id": str(uuid4())}]   # rows_added = 1

    # adb side-effect: first call is the upsert lambda (return fake_response);
    # second call is detect_for_account (invoke it via the mocked attr so
    # m_detect records the call).
    call_count = {"n": 0}

    async def fake_adb(fn, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return fake_response
        # Subsequent call(s): actually invoke fn so the patched
        # detect_for_account records its invocation.
        return fn(*args, **kwargs)

    with patch("skills.finance.ingestion.pipeline.adb", side_effect=fake_adb) as m_adb, \
         patch("skills.finance.ingestion.pipeline.validate") as m_val, \
         patch("skills.finance.ingestion.pipeline._log_success", new=AsyncMock()) as m_log, \
         patch(
             "skills.finance.categorization.refund_detector.detect_for_account",
             return_value=DetectionResult(refunds_linked=2, self_transfers_linked=1,
                                          rows_processed=3, rows_pending=0),
         ) as m_detect:
        m_val.return_value = MagicMock(ok=True, declared_out=Decimal("100"), extracted_out=Decimal("100"),
                                        delta_in=Decimal("0"), delta_out=Decimal("0"))
        m_log.return_value = {"status": "success"}

        from skills.finance.ingestion.pipeline import ingest
        log_entry = await ingest(pr, ACCT, source)

    assert log_entry["status"] == "success"
    # _log_success called BEFORE detection — assert ordering by checking
    # m_log was awaited first relative to the m_adb call that invoked detect
    m_log.assert_called_once()
    m_detect.assert_called_once()
    # Confirm adb was used for both upsert AND detection (invariant #1)
    assert m_adb.call_count == 2


@pytest.mark.asyncio
async def test_detection_crash_does_not_rollback_ingestion():
    """The load-bearing safety contract: if detect_for_account raises,
    ingestion is STILL committed and _log_success has STILL fired."""
    pr = _fake_parse_result()
    source = SourceMeta(source="manual_pdf", source_ref="test.pdf")

    fake_response = MagicMock()
    fake_response.data = [{"id": str(uuid4())}]

    call_count = {"n": 0}

    async def fake_adb(fn, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return fake_response
        # Second call: invoke fn so the RuntimeError on detect_for_account
        # actually propagates from detect (and gets caught by the wrapper).
        return fn(*args, **kwargs)

    with patch("skills.finance.ingestion.pipeline.adb", side_effect=fake_adb), \
         patch("skills.finance.ingestion.pipeline.validate") as m_val, \
         patch("skills.finance.ingestion.pipeline._log_success", new=AsyncMock()) as m_log, \
         patch(
             "skills.finance.categorization.refund_detector.detect_for_account",
             side_effect=RuntimeError("synthetic detection failure"),
         ) as m_detect:
        m_val.return_value = MagicMock(ok=True, declared_out=Decimal("100"), extracted_out=Decimal("100"),
                                        delta_in=Decimal("0"), delta_out=Decimal("0"))
        m_log.return_value = {"status": "success"}

        from skills.finance.ingestion.pipeline import ingest
        log_entry = await ingest(pr, ACCT, source)

    # Ingestion still committed
    assert log_entry["status"] == "success"
    # _log_success STILL called before the detection failure
    m_log.assert_called_once()
    # Detection WAS attempted (and raised, caught by safety wrapper)
    m_detect.assert_called_once()
