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

import logging
import re
from decimal import Decimal
from typing import Any

import pandas as pd

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

    Self-transfer = "Money sent to <person>" prefix AND the other-details column
    contains one of the user's own UPI handles. Paytm's Summary footnote says
    "Self transfer payments are not included" in declared paid total — so
    classifier results feed into the parser's declared_totals adjustment.
    """
    if not own_handles:
        return False
    if transaction_details is None or other_transaction_details is None:
        return False
    td = str(transaction_details)
    if not td.startswith("Money sent to "):
        return False
    other = str(other_transaction_details)
    return any(h in other for h in own_handles)


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
                    found[key] = _decimal_from_indian_str(value)
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
}


def _infer_direction(transaction_details: str) -> str:
    """Map the Transaction Details column prefix to direction. Raises if the
    prefix is unknown — Paytm's known prefix list is small and stable; an
    unknown prefix is a parser-update signal."""
    for prefix, direction in _DIRECTION_PREFIXES.items():
        if transaction_details.startswith(prefix):
            return direction
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
