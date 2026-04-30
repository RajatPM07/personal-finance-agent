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

import dataclasses
import hashlib
import logging
import re
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pdfplumber
import pikepdf

from skills.finance.ingestion._common import (
    ParsedRow,
    ParseResult,
    _decimal_from_indian_str,
)

logger = logging.getLogger(__name__)

__parser_version__ = "icici-savings-pdf/v2"


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


# Boundary markers
_NOMINEE_HEADER_RE = re.compile(
    r"^ACCOUNT\s+TYPE\s+ACCOUNT\s+NUMBER\s+MICR\s+CODE\s+IFS\s+CODE",
    re.IGNORECASE,
)

# Date prefix on a transaction line: "DD-MM-YYYY <rest>"
_DATE_LINE_RE = re.compile(r"^(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<rest>.*)$")
# Indian-format decimal number token (e.g. "1,86,062.00", "0.00")
_NUM_TOKEN_RE = re.compile(r"^[0-9,]+\.\d{2}$")

# MODE detection. ICICI's "MODE" column collapses to blank on text extraction,
# so MODE has to be inferred from either the head continuation line (e.g. UPI
# rows wrap with "UPI/..." above the date) or the first inline token after
# the date (e.g. "B/F", "BIL/...", "ACH/...").
_MODE_HEAD_PREFIXES = (
    "UPI/", "NEFT-", "NEFT/", "IMPS/", "IMPS-", "ACH/", "ACH-",
    "BIL/", "BIL-", "ATM/", "ATM-", "RTGS/", "INT.", "TFR/", "TFR-",
    "MAT/", "VAT/", "NFS/", "EBA/", "ISEC/", "VPS/", "IPS/",
    "TOP/", "INF/", "INF-", "SAL/", "SAL-",
)
_INLINE_MODE_TOKENS = {
    "B/F", "C/F", "BIL", "ATM", "ACH", "NEFT", "IMPS", "RTGS", "TFR",
    "SAL", "INF", "MAT", "MAB", "NFS", "EBA", "ISEC", "VPS", "IPS",
    "TOP", "UPI", "INT.PD", "INT",
}


def _detect_mode(head_continuation: str, inline_first_token: str) -> str:
    """Resolve the MODE for a transaction. Head wins (typical UPI case);
    falls back to the first inline token of the date row.

    Special case (added in v2): quarterly interest credits use a reference-
    prefixed colon layout — `<numeric-ref>:Int.Pd:<from>-<to>` — where the
    MODE token is wedged between colons. We scan the colon-split segments
    for a known mode name before falling through to the base extraction,
    so this row is labeled `INT.PD` instead of the leaked reference number.
    """
    if head_continuation:
        for p in _MODE_HEAD_PREFIXES:
            if head_continuation.startswith(p):
                return p.rstrip("/-.").upper()
    t = (inline_first_token or "").strip()
    if not t:
        return ""
    if ":" in t:
        known = {x.upper() for x in _INLINE_MODE_TOKENS}
        for seg in t.split(":"):
            s = seg.strip().rstrip(".").upper()
            if s in known:
                return s
    # Preserve B/F and C/F literally
    if t.startswith("B/F"):
        return "B/F"
    if t.startswith("C/F"):
        return "C/F"
    base = t.split("/")[0].split("-")[0].rstrip(".")
    if base.upper() in {x.upper() for x in _INLINE_MODE_TOKENS}:
        return base.upper()
    return base.upper()


def _parse_date_tokens(
    line: str,
) -> tuple[str, str, list[Decimal], str] | None:
    """Parse a date-prefixed line into (date_str, particulars_inline,
    trailing_nums, inline_first_token). Returns None if `line` doesn't start
    with DD-MM-YYYY."""
    if not line:
        return None
    m = _DATE_LINE_RE.match(line.strip())
    if not m:
        return None
    rest = m.group("rest")
    tokens = rest.split()
    nums: list[Decimal] = []
    while tokens and _NUM_TOKEN_RE.match(tokens[-1]):
        nums.insert(0, _decimal_from_indian_str(tokens.pop()))
    inline_particulars = " ".join(tokens)
    inline_first = tokens[0] if tokens else ""
    return (m.group("date"), inline_particulars, nums, inline_first)


def _extract_data_row(
    line: str,
    prev_balance: Decimal | None = None,
    head_continuation: str = "",
) -> ParsedRow | None:
    """Parse a single date-prefixed line into a ParsedRow.

    The number of trailing Indian-format numerics on the date line
    determines how the line is interpreted (because pdfplumber's text
    extraction collapses ICICI's empty deposit/withdrawal column to blank,
    so a typical txn line shows 2 numerics, not 3):

      * 1 numeric  → balance only (B/F or C/F) → returns None
                     (no monetary content; caller still uses balance for
                     `prev_balance` carry-forward).
      * 2 numerics → amount + balance. Direction inferred from balance delta
                     vs `prev_balance` (delta > 0 → 'in', else → 'out').
                     If `prev_balance` is None (first txn / standalone test),
                     defaults to 'out'.
      * 3 numerics → deposits + withdrawals + balance. Direction taken from
                     whichever column is non-zero. Both-zero → returns None.

    `head_continuation` is the wrapped PARTICULARS prefix that appeared on
    the line above the date row (typical for UPI rows). It seeds MODE
    detection and gets prepended to raw_merchant.

    Returns None for non-date lines.
    """
    parsed = _parse_date_tokens(line)
    if parsed is None:
        return None
    date_str, particulars_inline, nums, inline_first = parsed
    if not nums or len(nums) == 1:
        return None

    direction: Literal["in", "out"]
    if len(nums) == 2:
        amount, balance = nums
        direction = (
            "in"
            if prev_balance is not None and balance > prev_balance
            else "out"
        )
    elif len(nums) == 3:
        deposits, withdrawals, _balance = nums
        if deposits == 0 and withdrawals == 0:
            return None
        if deposits > 0 and withdrawals > 0:
            raise ParserError(
                f"Row has both deposits and withdrawals non-zero: {line!r}. "
                f"Layout drift likely."
            )
        if deposits > 0:
            direction = "in"
            amount = deposits
        else:
            direction = "out"
            amount = withdrawals
    else:
        raise ParserError(
            f"Unexpected {len(nums)} trailing numerics on date line: {line!r}"
        )

    mode = _detect_mode(head_continuation, inline_first)
    full_particulars = (
        f"{head_continuation} {particulars_inline}".strip()
        if head_continuation
        else particulars_inline
    )
    return ParsedRow(
        txn_date=_parse_savings_date(date_str),
        amount=amount,
        direction=direction,
        raw_merchant=full_particulars,
        source_row_ordinal=0,
        txn_mode=mode,
        is_upi_skip=classify_upi_skip(mode=mode, particulars=full_particulars),
    )


def _looks_like_header_or_chrome(line: str) -> bool:
    """Return True for lines that are clearly NOT continuation content.
    Conservative: when in doubt, return False (treat as continuation)."""
    upper = line.upper()
    if upper.startswith("DATE MODE PARTICULARS"):
        return True
    if upper.startswith("STATEMENT SUMMARY"):
        return True
    if upper.startswith("STATEMENT OF "):
        return True
    if upper.startswith("ACCOUNT HOLDERS"):
        return True
    if upper.startswith("ACCOUNT TYPE"):
        return True
    if "ICICI BANK LTD" in upper:
        return True
    return upper.startswith("PAGE ")


def _assemble_rows(lines: list[str]) -> list[ParsedRow]:
    """State machine — walk the flat line list and build ordinal-numbered
    ParsedRows.

    For each line:
      - Nominee block header → stop processing (last transaction page is over).
      - Total: subtotal row → flush pending continuations, exit table mode.
      - Date-prefixed line → the LAST pending continuation (if any) is the
        head of THIS txn; earlier pending lines are tail of the PREVIOUS
        txn (appended to its raw_merchant). Emit this txn with the next
        ordinal. Update prev_balance from the date line's trailing balance.
      - Other line → buffer as pending continuation (only once we've entered
        the table — chrome lines are filtered).

    Ordinals 1..N in declaration order.
    """
    rows: list[ParsedRow] = []
    pending: list[str] = []
    prev_balance: Decimal | None = None
    in_table = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if _NOMINEE_HEADER_RE.match(line):
            break

        if _parse_total_row(line) is not None:
            in_table = False
            pending = []
            continue

        date_parsed = _parse_date_tokens(line)
        if date_parsed is not None:
            in_table = True
            _, _, nums, _ = date_parsed
            head = pending[-1] if pending else ""
            tails_of_prev = pending[:-1]
            if tails_of_prev and rows:
                prev = rows[-1]
                rows[-1] = dataclasses.replace(
                    prev,
                    raw_merchant=(
                        prev.raw_merchant + " " + " ".join(tails_of_prev)
                    ).strip(),
                )
            parsed_row = _extract_data_row(
                line, prev_balance=prev_balance, head_continuation=head
            )
            if parsed_row is not None:
                rows.append(
                    dataclasses.replace(parsed_row, source_row_ordinal=len(rows) + 1)
                )
            if nums:
                prev_balance = nums[-1]
            pending = []
            continue

        # Plain continuation candidate
        if not (in_table or rows):
            continue
        if _looks_like_header_or_chrome(line):
            continue
        pending.append(line)

    # Final flush: any leftover continuations attach to the last emitted row.
    if pending and rows:
        prev = rows[-1]
        rows[-1] = dataclasses.replace(
            prev,
            raw_merchant=(prev.raw_merchant + " " + " ".join(pending)).strip(),
        )

    return rows


# Per-page subtotal: "Total: <deposits> <withdrawals> <balance>"
_TOTAL_ROW_RE = re.compile(
    r"^\s*Total\s*:\s*"
    r"(?P<deposits>[0-9,]+\.\d{2})\s+"
    r"(?P<withdrawals>[0-9,]+\.\d{2})\s+"
    r"(?P<balance>[0-9,]+\.\d{2})\s*$",
    re.IGNORECASE,
)


def _parse_total_row(line: str) -> tuple[Decimal, Decimal, Decimal] | None:
    """Parse 'Total: <deposits> <withdrawals> <balance>' line. Returns
    (deposits, withdrawals, balance) or None if line doesn't match."""
    m = _TOTAL_ROW_RE.match(line)
    if not m:
        return None
    return (
        _decimal_from_indian_str(m.group("deposits")),
        _decimal_from_indian_str(m.group("withdrawals")),
        _decimal_from_indian_str(m.group("balance")),
    )


def _aggregate_totals(lines: list[str]) -> dict[str, Any]:
    """Scan all lines for per-page Total: rows; aggregate to statement totals.

    Returns a dict matching the validator's expected shape:
        {'total_spends', 'total_credits', 'closing_balance',
         '_derived_from_rows': False}

    Raises ParserError if no Total: row is found across ANY page (indicates
    layout drift; ICICI savings statements always have explicit subtotals).
    """
    total_in = Decimal("0")
    total_out = Decimal("0")
    last_balance: Decimal | None = None
    for line in lines:
        parsed = _parse_total_row(line)
        if parsed is None:
            continue
        deposits, withdrawals, balance = parsed
        total_in += deposits
        total_out += withdrawals
        last_balance = balance
    if last_balance is None:
        raise ParserError(
            "No 'Total:' subtotal rows found in any page. ICICI Savings layout "
            "drift suspected; check the PDF text extraction for changes."
        )
    return {
        "total_spends": total_out,
        "total_credits": total_in,
        "closing_balance": last_balance,
        "_derived_from_rows": False,
    }


def _sha256_file(path: Path) -> str:
    """Hash the original (still-encrypted) PDF bytes. Re-decrypting with pikepdf
    produces a different byte stream, so we hash the input file directly to
    keep the import_hash stable across re-ingestion attempts."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse(pdf_path: Path, password: str) -> ParseResult:
    """Decrypt the ICICI Savings PDF and extract rows + page-subtotal totals.

    Steps:
      1. Hash the (still-encrypted) source PDF for stable import_hash.
      2. Decrypt to a temp file via pikepdf.
      3. pdfplumber-extract text from every page; flatten into a list of lines.
      4. _assemble_rows produces ParsedRows with multi-line PARTICULARS handling
         and UPI-skip flagging.
      5. _aggregate_totals scans the same lines for 'Total:' rows and sums
         them across pages → declared_totals.
    """
    pdf_path = Path(pdf_path)
    pdf_content_hash = _sha256_file(pdf_path)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        with pikepdf.open(pdf_path, password=password) as src:
            src.save(tmp.name)

        all_lines: list[str] = []
        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                all_lines.extend(text.splitlines())

    rows = _assemble_rows(all_lines)
    declared_totals = _aggregate_totals(all_lines)

    return ParseResult(
        rows=rows,
        declared_totals=declared_totals,
        pdf_content_hash=pdf_content_hash,
        parser_version=__parser_version__,
    )
