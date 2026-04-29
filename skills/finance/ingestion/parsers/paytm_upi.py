"""Paytm UPI XLSX statement parser — deterministic.

Paytm exports an unencrypted .xlsx with two sheets:
  - 'Summary': declared totals + per-source-account breakdown
  - 'Passbook Payment History': transaction rows

Spec: docs/superpowers/specs/2026-04-29-paytm-xlsx-ingestion-design.md

Three Paytm-specific behaviors:
  D1: rows where Your Account = "American Express Credit Card" are flagged
      with is_amex_routed=True. They appear in result.rows so the validator
      can verify we extracted everything Paytm reports, but pipeline drops
      them at insert via result.insertable_rows().
  D2: 'Money sent to ...' rows whose Other Transaction Details column
      contains a known own-handle are flagged is_self_transfer=True. They
      ARE inserted (audit trail) but the parser pre-adjusts declared_totals
      so the existing validator math closes (Summary excludes self-transfers
      from declared paid; we add their total back into total_spends).
  D4: Paytm's Tags column is captured into ParsedRow.category_hint with
      leading emoji stripped. NULL when the Tags column is empty.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pandas as pd

from skills.finance.ingestion._common import ParsedRow, ParseResult
from skills.finance.lib.db import service_client

logger = logging.getLogger(__name__)

__parser_version__ = "paytm-upi-xlsx/v1"


class ParserError(Exception):
    """Raised when the Paytm XLSX layout is unrecognized or row parsing fails."""


def classify_self_transfer(
    transaction_details: Any,
    other_transaction_details: Any,
    own_handles: list[str],
) -> bool:
    """Return True iff this row represents money sent from the user to themselves.

    Two cases match:
      1. Paytm's explicit "Transferred to Self, <bank>" prefix — unconditional
         self-transfer (Paytm itself labels it).
      2. "Money sent to <person>" prefix AND the other-details column contains
         one of the user's own UPI handles — heuristic disambiguation against
         non-self transfers using the same prefix.

    Paytm's Summary footnote says "Self transfer payments are not included" in
    declared paid totals — so classifier results feed into the parser's
    declared_totals adjustment.
    """
    if transaction_details is None:
        return False
    td = str(transaction_details)

    # Case 1: explicit Paytm-labelled self-transfer
    if td.startswith("Transferred to Self"):
        return True

    # Case 2: heuristic match on "Money sent to" + own-handle
    if td.startswith("Money sent to "):
        if not own_handles or other_transaction_details is None:
            return False
        other = str(other_transaction_details)
        return any(h in other for h in own_handles)

    return False


# Summary-sheet parsing ------------------------------------------------------

def _decimal_from_indian_str(s: Any) -> Decimal:
    """Convert '1,23,456.78' or 1234.56 or Decimal(...) → Decimal('1234.56').
    Tolerates Indian-style multi-comma thousand/lakh separators."""
    if isinstance(s, Decimal):
        return s
    cleaned = str(s).replace(",", "").strip()
    return Decimal(cleaned)


_SUMMARY_LABELS = {
    "paid_amount": "Money Paid (Amount in Rs.)",
    "paid_count": "Money Paid (No. of Payments)",
    "recv_amount": "Money Received (Amount in Rs.)",
    "recv_count": "Money Received (No. of Payments)",
}


def _read_summary_totals(summary_df: pd.DataFrame) -> dict:
    """Scan the Summary sheet for the four declared-total labels (label-scan
    rather than fixed-row indexing in case Paytm shifts the layout). Returns:
        {paid_amount: Decimal, paid_count: int,
         recv_amount: Decimal, recv_count: int}
    Raises ParserError if any of the four labels is not found.
    """
    found: dict = {}
    for i in range(len(summary_df)):
        label = summary_df.iat[i, 0]
        if label is None:
            continue
        label_str = str(label).strip()
        for key, target in _SUMMARY_LABELS.items():
            if key in found:
                continue
            if label_str == target:
                value = summary_df.iat[i, 1]
                if key.endswith("_amount"):
                    # Paytm Summary cells use signed magnitudes ('-' for paid,
                    # '+' for received). Our domain stores magnitudes only;
                    # direction is carried separately on each ParsedRow. So
                    # always coerce to positive here.
                    found[key] = abs(_decimal_from_indian_str(value))
                else:  # _count
                    found[key] = int(value)
                break
    missing = [k for k in _SUMMARY_LABELS if k not in found]
    if missing:
        raise ParserError(
            f"Paytm Summary sheet missing expected labels: {missing}. "
            f"Looked for: {list(_SUMMARY_LABELS.values())}. "
            f"First 15 rows of column A: "
            f"{[summary_df.iat[i, 0] for i in range(min(15, len(summary_df)))]}"
        )
    return found


# Direction inference + tag normalization ------------------------------------

_DIRECTION_PREFIXES: dict[str, str] = {
    "Paid to ": "out",
    "Money sent to ": "out",
    "Received from ": "in",
    # Real-fixture variants surfaced 2026-04-30 during plan execution.
    # Subscriptions / bill payments / refunds / wallet self-transfers — Paytm
    # uses distinct prefix families for these flows in the Passbook export.
    "Automatic payment for ": "out",   # recurring (Google Play, Apple, electricity, gas, ...)
    "Automatic payment of ": "out",    # variant of above (e.g. "Automatic payment of ₹399 setup for X")
    "Bill Payment for ": "out",         # one-off utility bill payments via Paytm bills
    "Bill Payment of ": "out",          # variant: CC bill payments ("Bill Payment of ICICI Bank Credit Card ...")
    "Refund for ": "in",                # money coming back from a previous "Paid to"
    "Transferred to Self": "out",       # Paytm-wallet → own bank account (handled in classify_self_transfer too)
}


def _infer_direction(transaction_details: str, amount_str: str | None = None) -> str:
    """Map the Transaction Details column prefix to direction. If `amount_str`
    is supplied AND the prefix is unknown, fall back to the leading sign on
    the Amount column (Paytm encodes direction there too: '+' → in, '-' → out).

    Raises ParserError only if BOTH the prefix is unknown AND the amount
    has no leading sign. Real-fixture orphan rows (e.g. a bare-name row
    'Vikhyat Sharma' with Amount='+10.00') survive via the fallback."""
    for prefix, direction in _DIRECTION_PREFIXES.items():
        if transaction_details.startswith(prefix):
            return direction
    if amount_str is not None:
        s = str(amount_str).strip()
        if s.startswith("+"):
            return "in"
        if s.startswith("-"):
            return "out"
    raise ParserError(
        f"Unknown Paytm Transaction Details prefix: {transaction_details!r}. "
        f"Known prefixes: {list(_DIRECTION_PREFIXES.keys())}. "
        f"If Paytm added a new pattern, extend _DIRECTION_PREFIXES."
    )


# Strip leading '#<emoji-or-symbol>' + optional whitespace; keep the label.
_TAG_PREFIX_RE = re.compile(r"^#\S+\s*")


def _strip_tag(tag_value: Any) -> str | None:
    """Convert Paytm's '#🥘 Food' → 'Food'. Returns None for blank/None input."""
    if tag_value is None:
        return None
    s = str(tag_value).strip()
    if not s:
        return None
    stripped = _TAG_PREFIX_RE.sub("", s).strip()
    return stripped or None


def _is_amex_routed(your_account: Any) -> bool:
    """A Paytm row's `Your Account` column tells which underlying source funded
    the payment. When the source is the user's AMEX CC, the same spend ALSO
    appears in the AMEX statement (via the generic 'Paytm' merchant). To avoid
    double-counting, the pipeline drops these rows at insert. Spec D1.
    """
    if your_account is None:
        return False
    return "American Express" in str(your_account)


def _load_own_upi_handles() -> list[str]:
    """Read all UPI-typed account identifiers from the `accounts` table.

    Used by parse() to classify 'Money sent to ...' rows as self-transfers
    when the destination matches one of the user's own handles. Called inside
    parse() which already runs on a worker thread (folder_watcher dispatch
    uses asyncio.to_thread), so calling the sync supabase client directly is
    safe — no `adb()` wrap needed here."""
    resp = (
        service_client()
        .table("accounts")
        .select("identifier,type")
        .eq("type", "upi")
        .execute()
    )
    # supabase-py types resp.data as list[JSON]; the items are dicts in
    # practice — cast so mypy can resolve `r.get(...)` (lessons.md 2026-04-26).
    records = cast(list[dict[str, Any]], resp.data or [])
    return [r["identifier"] for r in records if r.get("identifier")]


# parse() integration --------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_paytm_date(s: Any) -> date:
    """Paytm dates may arrive as datetime (Excel date cell) or as 'DD/MM/YYYY'
    string. Try datetime first; fall back to DD/MM/YYYY (Indian format)."""
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ParserError(f"Could not parse Paytm date: {s!r}")


def _strip_prefix(transaction_details: str) -> str:
    """Strip the 'Paid to ' / 'Money sent to ' / 'Received from ' prefix
    to get just the merchant/person name for `raw_merchant`."""
    for prefix in _DIRECTION_PREFIXES:
        if transaction_details.startswith(prefix):
            return transaction_details[len(prefix):].strip()
    return transaction_details


def parse(file_path: Path) -> ParseResult:
    """Parse a Paytm UPI XLSX export.

    Behavior:
      - Reads 'Summary' sheet for declared paid + received totals.
      - Reads 'Passbook Payment History' sheet for transaction rows.
      - Flags AMEX-routed rows (D1) and self-transfer rows (D2).
      - Adjusts declared_totals so the existing single-tolerance validator
        passes against `result.rows` (which includes AMEX-routed AND
        self-transfers — see spec §7.1):
            extracted_out  = ordinary + amex_routed + self_transfer
            declared_total_spends_published
                          = paid_summary + self_transfer_total_we_computed
                          = (ordinary + amex_routed) + self_transfer
                          = extracted_out  ✓
      - Populates category_hint from Tags column with leading emoji stripped (D4).
    """
    file_path = Path(file_path)
    pdf_content_hash = _sha256_file(file_path)

    # 1) Declared totals from Summary sheet
    summary_df = pd.read_excel(file_path, sheet_name="Summary",
                               header=None, engine="openpyxl")
    summary = _read_summary_totals(summary_df)

    # 2) Transaction rows from Passbook
    passbook = pd.read_excel(file_path, sheet_name="Passbook Payment History",
                             engine="openpyxl")
    # Expected columns (verified during 2026-04-26 inspection):
    #   Date | Time | Transaction Details | Other Transaction Details |
    #   Your Account | Amount | UPI Ref No. | Order ID | Remarks | Tags | Comment
    required = {"Date", "Transaction Details", "Your Account", "Amount", "Tags"}
    missing = required - set(passbook.columns)
    if missing:
        raise ParserError(
            f"Paytm Passbook sheet missing required columns: {missing}. "
            f"Got columns: {list(passbook.columns)}"
        )

    own_handles = _load_own_upi_handles()

    rows: list[ParsedRow] = []
    self_transfer_total = Decimal("0")
    ordinal = 1
    for _, raw in passbook.iterrows():
        td = raw["Transaction Details"]
        if td is None or (isinstance(td, float) and pd.isna(td)):
            continue
        td = str(td)
        amt_raw = raw["Amount"]
        if amt_raw is None or (isinstance(amt_raw, float) and pd.isna(amt_raw)):
            continue
        try:
            direction = _infer_direction(td, amount_str=str(amt_raw))
        except ParserError:
            logger.warning("skipping unrecognized Paytm row: %r", td)
            continue
        amount = abs(_decimal_from_indian_str(amt_raw))

        date_raw = raw["Date"]
        if date_raw is None or (isinstance(date_raw, float) and pd.isna(date_raw)):
            continue

        is_amex = _is_amex_routed(raw["Your Account"])
        is_self = classify_self_transfer(
            transaction_details=td,
            other_transaction_details=raw.get("Other Transaction Details"),
            own_handles=own_handles,
        )
        if is_self and direction == "out":
            self_transfer_total += amount

        rows.append(ParsedRow(
            txn_date=_parse_paytm_date(date_raw),
            amount=amount,
            direction=direction,  # type: ignore[arg-type]
            raw_merchant=_strip_prefix(td),
            source_row_ordinal=ordinal,
            is_amex_routed=is_amex,
            is_self_transfer=is_self,
            category_hint=_strip_tag(raw.get("Tags")),
        ))
        ordinal += 1

    # 3) Adjust declared totals so the existing validator passes against
    #    result.rows (see docstring math). Self-transfers add to total_spends;
    #    AMEX-routed rows are kept in result.rows so they contribute to
    #    extracted_out and the math closes.
    declared_total_spends = summary["paid_amount"] + self_transfer_total
    declared_total_credits = summary["recv_amount"]

    return ParseResult(
        rows=rows,
        declared_totals={
            "total_spends": declared_total_spends,
            "total_credits": declared_total_credits,
            "closing_balance": None,
            "_derived_from_rows": False,
        },
        pdf_content_hash=pdf_content_hash,
        parser_version=__parser_version__,
    )
