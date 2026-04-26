"""ICICI CC PDF statement parser — deterministic.

Uses pikepdf for password decryption + pdfplumber for text extraction +
regex parsing of the transaction-table region. Calibrated against the real
ICICI CC statement format (verified against fixture 2026-04-26).

Known weakness — declared totals derivation:
ICICI's STATEMENT SUMMARY block doesn't print a single, parseable
"Total Spends" / "Total Credits" label-value pair. It prints a fragmented
balance summary ("Total Amount due = previous balance + purchases +
cash advances - payments") split across multiple text lines mixed with
credit-limit and reward-points data. Position-based extraction would be
extremely brittle.

Pragmatic fallback (matches AMEX no-Total-row behaviour, spec §9.2):
- Try regex matches for common total labels first (TOTAL_SPENDS_RE etc.)
- If no match, derive declared_totals from the row sums themselves.
  Validator passes tautologically; `declared_totals['_derived_from_rows']
  = True` flag propagates to ingestion_log so the user sees the
  annotation in the success message.
- Trades off the integrity-check guarantee for ICICI in exchange for
  not silently rejecting every statement. Future calibration may add
  position-based total extraction; for now, the row-level extraction is
  the integrity surface.

Re-calibration required if ICICI changes the layout — bump
__parser_version__ when that happens.
"""
from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pikepdf

from skills.finance.ingestion._common import ParsedRow, ParseResult

logger = logging.getLogger(__name__)

# CLAUDE.md invariant #4: manually curated. Bump only when the parser's
# extracted output shape would change for the same input.
__parser_version__ = "icici-cc/v1"

# Row pattern: DATE [SERNO] MERCHANT [REWARD_PTS] AMOUNT [CR]
# `merchant` is non-greedy and consumes everything between the date and
# the trailing amount. Optional `CR` suffix is case-insensitive (the
# fixture shows `CR` not `Cr`).
ROW_RE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<merchant>.+?)\s+"
    r"(?P<amount>[\d,]+\.\d{2})"
    r"(?:\s*(?P<cr>CR|Cr))?\s*$"
)

# Best-effort declared-totals labels. ICICI variants observed in fixtures
# use no single canonical label. If none match, the parser falls back to
# row-derived totals (see module docstring).
TOTAL_SPENDS_RE = re.compile(r"Total\s+(?:Purchases|Spends|Debits)\s*[:.\s]*([\d,]+\.\d{2})", re.IGNORECASE)
TOTAL_CREDITS_RE = re.compile(r"Total\s+(?:Credits|Payments)\s*[:.\s]*([\d,]+\.\d{2})", re.IGNORECASE)
CLOSING_BAL_RE = re.compile(r"(?:Closing\s+Balance|Total\s+Amount\s+Due)\s*[:.\s]*[`₹]?\s*([\d,]+\.\d{2})", re.IGNORECASE)


def _parse_amount(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%d/%m/%Y").date()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse(pdf_path: Path, password: str) -> ParseResult:
    """Decrypt the ICICI CC statement and extract rows + declared totals."""
    pdf_path = Path(pdf_path)
    pdf_content_hash = _sha256_file(pdf_path)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        with pikepdf.open(pdf_path, password=password) as src:
            src.save(tmp.name)

        rows: list[ParsedRow] = []
        all_text_parts: list[str] = []
        ordinal = 1
        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                all_text_parts.append(text)
                for line in text.splitlines():
                    m = ROW_RE.match(line.strip())
                    if not m:
                        continue
                    amt = _parse_amount(m.group("amount"))
                    if amt <= 0:
                        # Skip ₹0.00 rows — these are reward-only / status /
                        # placeholder entries that pdfplumber surfaces as
                        # transaction-like text but have no monetary value.
                        # Real transactions are non-zero by definition.
                        continue
                    rows.append(
                        ParsedRow(
                            txn_date=_parse_date(m.group("date")),
                            amount=amt,
                            direction="in" if m.group("cr") else "out",
                            raw_merchant=m.group("merchant").strip(),
                            source_row_ordinal=ordinal,
                        )
                    )
                    ordinal += 1

        full_text = "\n".join(all_text_parts)

    spends_m = TOTAL_SPENDS_RE.search(full_text)
    credits_m = TOTAL_CREDITS_RE.search(full_text)
    cb_m = CLOSING_BAL_RE.search(full_text)

    if spends_m:
        # Regex match found — use declared values (full integrity check active)
        declared_totals: dict = {
            "total_spends": _parse_amount(spends_m.group(1)),
            "total_credits": _parse_amount(credits_m.group(1)) if credits_m else Decimal("0"),
            "closing_balance": _parse_amount(cb_m.group(1)) if cb_m else None,
            "_derived_from_rows": False,
        }
    else:
        # Fallback: derive declared totals from row sums (validator passes
        # tautologically; success message annotates this). Same pattern as
        # AMEX no-Total-row case (spec §9.2).
        derived_out = sum((r.amount for r in rows if r.direction == "out"), Decimal("0"))
        derived_in = sum((r.amount for r in rows if r.direction == "in"), Decimal("0"))
        declared_totals = {
            "total_spends": derived_out,
            "total_credits": derived_in,
            "closing_balance": _parse_amount(cb_m.group(1)) if cb_m else None,
            "_derived_from_rows": True,
        }
        logger.warning(
            "ICICI CC parser: no 'Total Spends' label found in PDF text; "
            "deriving declared totals from row sums (validator will pass "
            "tautologically; success message annotates this)"
        )

    return ParseResult(
        rows=rows,
        declared_totals=declared_totals,
        pdf_content_hash=pdf_content_hash,
        parser_version=__parser_version__,
    )
