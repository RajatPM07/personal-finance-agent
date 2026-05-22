"""Ingestion pipeline orchestrator.

Runs: validate → compute Mode-B import_hash per row → upsert to transactions →
log to ingestion_log → return status. On totals failure, NO rows are inserted
(per spec §8 / PRD §18.4 — entire statement rejected).

Caller (folder_watcher.dispatch_to_parser) handles the Telegram summary
message after this returns. Pipeline stays pure (no Telegram I/O).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from skills.finance.ingestion._common import (
    RAJAT_USER_ID,
    ParsedRow,
    ParseResult,
    SourceMeta,
    ValidationResult,
)
from skills.finance.ingestion.statement_validator import validate
from skills.finance.lib.db import adb, service_client
from skills.finance.lib.hashing import import_hash_pdf

logger = logging.getLogger(__name__)


def _build_insert_row(
    r: ParsedRow,
    account_id: UUID,
    pr: ParseResult,
    source: SourceMeta,
) -> dict[str, Any]:
    # normalized_description = raw_merchant in Week 2; merchant normalization
    # lands in Week 5 with a parser_version bump that forces re-ingest of
    # affected rows.
    h = import_hash_pdf(
        account_id=str(account_id),
        txn_date=r.txn_date,
        amount=r.amount,
        normalized_description=r.raw_merchant,
        pdf_content_hash=pr.pdf_content_hash,
        source_row_ordinal=r.source_row_ordinal,
        parser_version=pr.parser_version,
    )
    return {
        "user_id": RAJAT_USER_ID,
        "account_id": str(account_id),
        "date": r.txn_date.isoformat(),
        "amount": str(r.amount),
        "currency": "INR",
        "direction": r.direction,
        "raw_merchant": r.raw_merchant,
        "source": source.source,
        "source_ref": source.source_ref,
        "pdf_content_hash": pr.pdf_content_hash,
        "source_row_ordinal": r.source_row_ordinal,
        "parser_version": pr.parser_version,
        "import_hash": h,
        "category_hint": r.category_hint,    # W3.1: Paytm-only today; NULL for ICICI/AMEX
        "txn_mode": r.txn_mode,              # W3.4: ICICI Savings-only; NULL for ICICI CC/AMEX/Paytm
    }


async def _log_validation_failure(
    pr: ParseResult,
    source: SourceMeta,
    val: ValidationResult,
) -> dict[str, Any]:
    log_row: dict[str, Any] = {
        "source": source.source,
        "source_ref": source.source_ref,
        "status": "total_check_failed",
        "rows_added": 0,
        "declared_total": str(val.declared_out),
        "extracted_total": str(val.extracted_out),
        "error_msg": (
            f"Totals mismatch: delta_in={val.delta_in}, delta_out={val.delta_out} "
            f"(tolerance ₹1). Statement NOT ingested."
        ),
    }
    await adb(
        lambda: service_client().table("ingestion_log").insert(log_row).execute()
    )
    return log_row


async def _log_success(
    pr: ParseResult,
    source: SourceMeta,
    val: ValidationResult,
    rows_added: int,
) -> dict[str, Any]:
    status = "success" if rows_added > 0 else "skipped_duplicate"
    derived = pr.declared_totals.get("_derived_from_rows", False)
    error_msg = "declared totals derived from row sums (no Total row in source)" if derived else None
    log_row: dict[str, Any] = {
        "source": source.source,
        "source_ref": source.source_ref,
        "status": status,
        "rows_added": rows_added,
        "declared_total": str(val.declared_out),
        "extracted_total": str(val.extracted_out),
        "error_msg": error_msg,
    }
    await adb(
        lambda: service_client().table("ingestion_log").insert(log_row).execute()
    )
    return log_row


async def ingest(
    parse_result: ParseResult,
    account_id: UUID,
    source_meta: SourceMeta,
) -> dict[str, Any]:
    """Orchestrate validate → upsert → log. Returns the ingestion_log row.

    Caller is responsible for sending the Telegram summary message; this fn
    keeps the pipeline pure (no Telegram I/O)."""
    val = validate(parse_result)
    if not val.ok:
        logger.warning(
            "validation failed for %s/%s: delta_in=%s delta_out=%s",
            source_meta.source, source_meta.source_ref,
            val.delta_in, val.delta_out,
        )
        return await _log_validation_failure(parse_result, source_meta, val)

    rows = [
        _build_insert_row(r, account_id, parse_result, source_meta)
        for r in parse_result.insertable_rows()
    ]

    response = await adb(
        lambda: service_client()
            .table("transactions")
            .upsert(rows, on_conflict="import_hash", ignore_duplicates=True)
            .execute()
    )
    rows_added = len(response.data) if response.data else 0
    logger.info(
        "ingested %d rows from %s/%s (validator ok, %d total)",
        rows_added, source_meta.source, source_meta.source_ref, len(rows),
    )
    log_entry = await _log_success(parse_result, source_meta, val, rows_added)

    if rows_added > 0:
        await _run_refund_detection_safe(account_id, parse_result)

    return log_entry


async def _run_refund_detection_safe(
    account_id: UUID, parse_result: ParseResult,
) -> None:
    """Inline refund + self-transfer detection. Per W5.1 spec §6.1: failures
    here MUST NOT roll back the ingestion that's already committed.
    AUDIT fields are derived; the next detection run picks up unprocessed rows."""
    try:
        from skills.finance.categorization.refund_detector import detect_for_account
        earliest_date = min(r.txn_date for r in parse_result.insertable_rows())
        since = earliest_date - timedelta(days=30)
        result = await adb(detect_for_account, account_id, since)
        logger.info(
            "refund detection: account=%s since=%s refunds=%d self_transfers=%d "
            "processed=%d pending=%d",
            account_id, since, result.refunds_linked, result.self_transfers_linked,
            result.rows_processed, result.rows_pending,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "refund detection failed for account=%s — ingestion already committed",
            account_id,
        )
