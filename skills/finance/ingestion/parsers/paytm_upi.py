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
from typing import Any

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
