"""ICICI Savings net-banking XLS transaction history parser — deterministic.

ICICI's net-banking "Transaction History" export (OpTransactionHistory*.xls)
is an unencrypted XLS file. This parser handles it as a complement to the
icici_savings PDF parser — same account, different download path.

Column layout (header detected dynamically):
  S No. | Value Date | Transaction Date | Cheque Number | Transaction Remarks
  | Withdrawal Amount(INR) | Deposit Amount(INR) | Balance(INR)

UPI rows are skipped at insert (is_upi_skip=True) — Paytm passbook is the
V1 source-of-truth for UPI activity (same D1 rule as the PDF parser).
Mode is inferred from the Transaction Remarks prefix.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from skills.finance.ingestion._common import (
    ParsedRow,
    ParseResult,
    _decimal_from_indian_str,
)
from skills.finance.ingestion.parsers.icici_savings import classify_upi_skip

logger = logging.getLogger(__name__)

__parser_version__ = "icici-savings-xls/v1"

_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

_MODE_HEAD_PREFIXES = (
    "UPI/", "NEFT-", "NEFT/", "IMPS/", "IMPS-", "ACH/", "ACH-",
    "BIL/", "BIL-", "ATM/", "ATM-", "RTGS/", "INT.", "TFR/", "TFR-",
    "MAT/", "NFS/", "EBA/", "ISEC/", "VPS/", "IPS/",
    "TOP/", "INF/", "INF-", "SAL/", "SAL-",
)
_INLINE_MODE_TOKENS = {
    "B/F", "C/F", "BIL", "ATM", "ACH", "NEFT", "IMPS", "RTGS", "TFR",
    "SAL", "INF", "MAT", "MAB", "NFS", "EBA", "ISEC", "VPS", "IPS",
    "TOP", "UPI", "INT.PD", "INT",
}


class ParserError(Exception):
    pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_date(s: Any) -> date:
    s = str(s).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ParserError(f"Cannot parse date: {s!r}")


def _detect_mode(remarks: str) -> str:
    for p in _MODE_HEAD_PREFIXES:
        if remarks.startswith(p):
            return p.rstrip("/-.").upper()
    first_token = remarks.split("/")[0].split("-")[0].strip()
    if ":" in first_token:
        known = {x.upper() for x in _INLINE_MODE_TOKENS}
        for seg in first_token.split(":"):
            s = seg.strip().rstrip(".").upper()
            if s in known:
                return s
    if first_token.upper() in {x.upper() for x in _INLINE_MODE_TOKENS}:
        return first_token.upper()
    return ""


def _find_header_row(df: pd.DataFrame) -> int:
    """Return the 0-based index of the row containing 'Transaction Remarks'."""
    for i, row in df.iterrows():
        if any(str(v).strip() == "Transaction Remarks" for v in row.values):
            return int(i)
    raise ParserError("Could not find header row with 'Transaction Remarks'")


def parse(xls_path: Path) -> ParseResult:
    """Extract rows from an ICICI net-banking XLS transaction history export."""
    xls_path = Path(xls_path)
    file_hash = _sha256_file(xls_path)

    df = pd.read_excel(xls_path, header=None, dtype=str)
    header_idx = _find_header_row(df)

    rows: list[ParsedRow] = []
    total_out = Decimal(0)
    total_in = Decimal(0)
    ordinal = 0

    for _, row in df.iloc[header_idx + 1:].iterrows():
        value_date_raw = str(row.iloc[2]).strip()
        if not _DATE_RE.match(value_date_raw):
            # Continuation row (long remarks split across lines) or footer — skip.
            # Amounts are always on the date-bearing row so nothing is lost.
            continue

        txn_date = _parse_date(str(row.iloc[3]).strip())  # Transaction Date col
        remarks = str(row.iloc[5]).strip()
        withdrawal_raw = str(row.iloc[6]).strip()
        deposit_raw = str(row.iloc[7]).strip()

        withdrawal = _decimal_from_indian_str(withdrawal_raw) if withdrawal_raw not in ("nan", "") else Decimal(0)
        deposit = _decimal_from_indian_str(deposit_raw) if deposit_raw not in ("nan", "") else Decimal(0)

        if withdrawal > 0 and deposit == 0:
            direction: str = "out"
            amount = withdrawal
        elif deposit > 0 and withdrawal == 0:
            direction = "in"
            amount = deposit
        else:
            # Both zero or both non-zero (unusual) — skip
            continue

        mode = _detect_mode(remarks)
        is_upi = classify_upi_skip(mode, remarks)

        ordinal += 1
        rows.append(ParsedRow(
            txn_date=txn_date,
            amount=amount,
            direction=direction,  # type: ignore[arg-type]
            raw_merchant=remarks,
            source_row_ordinal=ordinal,
            is_upi_skip=is_upi,
            txn_mode=mode or None,
        ))

        if direction == "out":
            total_out += amount
        else:
            total_in += amount

    logger.info(
        "icici_savings_xls: parsed %d rows (%d UPI-skipped) from %s",
        len(rows),
        sum(1 for r in rows if r.is_upi_skip),
        xls_path.name,
    )

    declared_totals = {
        "total_spends": total_out,
        "total_credits": total_in,
        "closing_balance": None,
        "_derived_from_rows": True,
    }
    logger.warning(
        "icici_savings_xls: declared totals derived from row sums; "
        "validator will pass tautologically."
    )

    return ParseResult(
        rows=rows,
        declared_totals=declared_totals,
        pdf_content_hash=file_hash,
        parser_version=__parser_version__,
    )
