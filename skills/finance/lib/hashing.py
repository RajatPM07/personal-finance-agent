from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal


def _normalize_amount(amount: float | Decimal) -> str:
    return f"{Decimal(str(amount)).quantize(Decimal('0.01'))}"


def _normalize_desc(s: str) -> str:
    return s.strip().lower()


def import_hash_time_bearing(
    account_id: str,
    txn_time: datetime,
    amount: float | Decimal,
    normalized_description: str,
    parser_version: str,
) -> str:
    """Mode A — sources that provide exact timestamps (SMS, Gmail txn emails).

    source_ref is intentionally NOT included so the same transaction observed via
    multiple time-bearing channels dedups to a single row.
    """
    if txn_time.tzinfo is None:
        raise ValueError("txn_time must be timezone-aware")
    parts = [
        account_id,
        txn_time.isoformat(),
        _normalize_amount(amount),
        _normalize_desc(normalized_description),
        parser_version,
    ]
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()


def import_hash_pdf(
    account_id: str,
    txn_date: date,
    amount: float | Decimal,
    normalized_description: str,
    pdf_content_hash: str,
    source_row_ordinal: int,
    parser_version: str,
) -> str:
    """Mode B — PDF-derived rows (CC statements, MF CAS, bank PDFs).

    - source_row_ordinal disambiguates intra-PDF same-day same-amount rows.
    - pdf_content_hash scopes uniqueness to the specific source document.
    - parser_version makes parser upgrades observable — re-parse with a bumped
      version produces fresh hashes rather than silently merging.
    - Rolling-statement overlap (same real txn in two different PDFs) is a known
      tradeoff handled by the secondary fuzzy pass (see PRD §7).
    """
    parts = [
        account_id,
        txn_date.isoformat(),
        _normalize_amount(amount),
        _normalize_desc(normalized_description),
        pdf_content_hash,
        str(source_row_ordinal),
        parser_version,
    ]
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()
