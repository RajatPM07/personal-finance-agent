"""ICICI Savings PDF e-statement parser — deterministic.

ICICI sends a password-protected PDF (same password convention as ICICI CC,
per credentials.yaml entries `icici_cc_<last4>` and `icici_savings_<last4>`).
Use pikepdf to decrypt + pdfplumber for text extraction + regex anchor scan
for the transaction table.

Spec: docs/superpowers/specs/2026-04-30-icici-savings-ingestion-design.md

Three Savings-specific behaviors:
  D1: rows where MODE=='UPI' OR PARTICULARS startswith 'UPI/' are flagged
      with is_upi_skip=True. They appear in result.rows so the validator
      can compare against page subtotals (which include UPI), but the
      pipeline drops them at insert via insertable_rows() because Paytm
      passbook is the V1 source-of-truth for UPI activity.
  D3: each ParsedRow carries the MODE column value into the new
      `txn_mode` field (UPI/NEFT/IMPS/ATM/BIL/PAY/SAL/INT.PD/TFR/etc.).
  D7: classify_upi_skip uses OR-logic across MODE and PARTICULARS for
      robustness against MODE-column extraction quirks.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from skills.finance.ingestion._common import (
    ParsedRow,
    _decimal_from_indian_str,
)

logger = logging.getLogger(__name__)

__parser_version__ = "icici-savings-pdf/v1"


class ParserError(Exception):
    """Raised when the savings PDF layout is unrecognized or row parsing fails."""


def classify_upi_skip(mode: Any, particulars: Any) -> bool:
    """Return True iff this row represents a UPI transaction that should be
    dropped at insert (Paytm passbook is the V1 source-of-truth for UPI).

    OR-logic across both signals — D7 in the savings spec:
      - MODE column normalized → "UPI"  (case-insensitive)
      - PARTICULARS starts with "UPI/"  (catches rows where MODE extraction is finicky)
    """
    if mode is not None:
        m = str(mode).strip().upper()
        if m == "UPI":
            return True
    if particulars is not None:
        p = str(particulars).strip()
        if p.startswith("UPI/"):
            return True
    return False


def _parse_savings_date(s: Any) -> date:
    """ICICI Savings statements use DD-MM-YYYY format. Tolerate ISO + slashed
    variants for defensiveness."""
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ParserError(f"Could not parse ICICI Savings date: {s!r}")


# Anchor pattern: "DD-MM-YYYY MODE PARTICULARS_TEXT NUMBER NUMBER NUMBER"
# - Date: 10 chars exactly
# - MODE: short alphanumeric token (UPI, NEFT, IMPS, ATM, BIL/PAY, SAL, INT.PD, TFR, etc.)
# - PARTICULARS: arbitrary text (lazy, terminated by the 3 trailing numerics)
# - DEPOSITS / WITHDRAWALS / BALANCE: Indian-format numerics like "1,23,456.78"
_ROW_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{4})\s+"
    r"(?P<mode>[A-Z][A-Z0-9./]*?)\s+"
    r"(?P<particulars>.+?)\s+"
    r"(?P<deposits>[0-9,]+\.\d{2})\s+"
    r"(?P<withdrawals>[0-9,]+\.\d{2})\s+"
    r"(?P<balance>[0-9,]+\.\d{2})$"
)


def _extract_data_row(line: str) -> ParsedRow | None:
    """Parse a single transaction line. Returns None if the line isn't a
    transaction row (header, page footer 'Total:', continuation line, blank,
    or both monetary columns are zero).

    Caller is responsible for assembling continuation lines onto the previous
    row's `raw_merchant` (Task 6) and for assigning the real ordinal.
    """
    if not line:
        return None
    m = _ROW_RE.match(line.strip())
    if not m:
        return None
    deposits = _decimal_from_indian_str(m.group("deposits"))
    withdrawals = _decimal_from_indian_str(m.group("withdrawals"))
    if deposits == 0 and withdrawals == 0:
        return None
    if deposits > 0 and withdrawals > 0:
        raise ParserError(
            f"Row has both deposits and withdrawals non-zero: {line!r}. "
            f"Layout drift likely; check ROW_RE column alignment."
        )
    direction: Literal["in", "out"]
    if deposits > 0:
        direction = "in"
        amount = deposits
    else:
        direction = "out"
        amount = withdrawals
    mode = m.group("mode")
    particulars = m.group("particulars").strip()
    return ParsedRow(
        txn_date=_parse_savings_date(m.group("date")),
        amount=amount,
        direction=direction,
        raw_merchant=particulars,
        source_row_ordinal=0,
        txn_mode=mode,
        is_upi_skip=classify_upi_skip(mode=mode, particulars=particulars),
    )


# Boundary markers — stop assembly when seeing the nominee block on the last
# transaction page (so we don't mistakenly grab nominee detail lines as
# continuations of the last txn).
_NOMINEE_HEADER_RE = re.compile(
    r"^ACCOUNT\s+TYPE\s+ACCOUNT\s+NUMBER\s+MICR\s+CODE\s+IFS\s+CODE",
    re.IGNORECASE,
)
_TOTAL_FOOTER_RE = re.compile(r"^\s*Total\s*:", re.IGNORECASE)


def _looks_like_header_or_chrome(line: str) -> bool:
    """Return True for lines that are clearly NOT continuation content.
    Conservative: when in doubt, return False (treat as continuation)."""
    upper = line.upper()
    if upper.startswith("DATE MODE PARTICULARS"):
        return True
    if upper.startswith("STATEMENT SUMMARY"):
        return True
    if upper.startswith("ACCOUNT HOLDERS"):
        return True
    if upper.startswith("ACCOUNT TYPE"):
        return True
    if "ICICI BANK LTD" in upper:
        return True
    return upper.startswith("PAGE ")


def _assemble_rows(lines: list[str]) -> list[ParsedRow]:
    """Walk a flat list of text lines and build ordinal-numbered ParsedRows.

    For each line, in order:
      - Data row → finalize the previous row, start a new one.
      - Boundary (Total: footer / nominee header) → finalize current.
      - Continuation candidate → append to the previous row's raw_merchant
        if a row exists; drop otherwise (no anchor to attach to).

    Ordinals are assigned 1..N in declaration order.
    """
    rows: list[ParsedRow] = []
    current: ParsedRow | None = None

    def finalize() -> None:
        nonlocal current
        if current is not None:
            ordinal = len(rows) + 1
            rows.append(
                ParsedRow(
                    txn_date=current.txn_date,
                    amount=current.amount,
                    direction=current.direction,
                    raw_merchant=current.raw_merchant.strip(),
                    source_row_ordinal=ordinal,
                    txn_mode=current.txn_mode,
                    is_upi_skip=current.is_upi_skip,
                )
            )
            current = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if _NOMINEE_HEADER_RE.match(line):
            finalize()
            break

        if _TOTAL_FOOTER_RE.match(line):
            finalize()
            continue

        candidate = _extract_data_row(line)
        if candidate is not None:
            finalize()
            current = candidate
            continue

        if current is not None and not _looks_like_header_or_chrome(line):
            current = ParsedRow(
                txn_date=current.txn_date,
                amount=current.amount,
                direction=current.direction,
                raw_merchant=(current.raw_merchant + " " + line).strip(),
                source_row_ordinal=current.source_row_ordinal,
                txn_mode=current.txn_mode,
                is_upi_skip=current.is_upi_skip
                or classify_upi_skip(mode=current.txn_mode, particulars=line),
            )

    finalize()
    return rows


def _sha256_file(path: Path) -> str:
    """Hash the original (still-encrypted) PDF bytes. Re-decrypting with pikepdf
    produces a different byte stream, so we hash the input file directly to
    keep the import_hash stable across re-ingestion attempts."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
