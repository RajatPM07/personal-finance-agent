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
from datetime import date, datetime
from pathlib import Path
from typing import Any

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


def _sha256_file(path: Path) -> str:
    """Hash the original (still-encrypted) PDF bytes. Re-decrypting with pikepdf
    produces a different byte stream, so we hash the input file directly to
    keep the import_hash stable across re-ingestion attempts."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
