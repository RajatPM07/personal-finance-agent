"""AMEX CC XLSX statement parser — deterministic.

AMEX's MyStatement portal exports an unencrypted .xlsx file. We use
pandas.read_excel + structured-cell extraction. No LLM in path.

Calibrated against fixture (Task 0 §0.1):
- Header row at index 6 (rows 0-5 are preamble: card title, holder name,
  masked account, blank). `find_header_row` walks the first 30 rows so
  this is handled automatically.
- Trailing fully-empty 11th column (XLSX export artifact). Dropped via
  df.dropna(axis=1, how='all').
- Date format MM/DD/YYYY (US-style on Indian-issued AMEX cards).
- Amount is single signed numeric: positive=charge (out), negative=refund (in).

KNOWN_COLUMN_SETS lists the layouts we've seen. If a real export uses
different headers, find_header_row raises ParserError with the actual
headers in the message — extend KNOWN_COLUMN_SETS and re-run.

Known weakness: AMEX exports may not include a "Total" / "Statement Total"
footer row. When absent, declared_totals is derived from row sums and
the validator passes tautologically. Logged as a warning. See spec §6.3.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from skills.finance.ingestion._common import ParsedRow, ParseResult

logger = logging.getLogger(__name__)

# CLAUDE.md invariant #4: manually curated.
__parser_version__ = "amex-cc-xlsx/v1"


class ParserError(Exception):
    """Raised when XLSX header detection fails or row parsing breaks."""


# Each entry maps semantic field → list of accepted column header variants
# (case-insensitive, whitespace-tolerant). The first set with all required
# keys present in a row wins.
KNOWN_COLUMN_SETS: list[dict[str, list[str]]] = [
    # India MyStatement, signed-amount layout (verified fixture, Task 0 §0.1)
    {
        "date": ["Date", "Transaction Date"],
        "description": ["Description", "Description of Transaction", "Details"],
        "amount": ["Amount"],
    },
    # Split debit/credit columns variant (defensive — not seen in fixture)
    {
        "date": ["Date", "Transaction Date"],
        "description": ["Description", "Description of Transaction", "Details"],
        "debit": ["Charges", "Debit"],
        "credit": ["Credits", "Credit"],
    },
]


def _normalize_header(s: Any) -> str:
    if pd.isna(s):
        return ""
    return str(s).strip().lower()


def find_header_row(df: pd.DataFrame) -> tuple[int, dict[str, str]]:
    """Walk the first 30 rows. For each, check if the values match any
    KNOWN_COLUMN_SETS entry. Return (row_index, mapping from semantic_key →
    column index as str).

    Raises ParserError with a preview of the first 10 rows if no match found."""
    max_rows = min(30, len(df))
    for i in range(max_rows):
        row_values = [_normalize_header(v) for v in df.iloc[i].values]
        for column_set in KNOWN_COLUMN_SETS:
            mapping: dict[str, str] = {}
            all_found = True
            for semantic_key, accepted in column_set.items():
                accepted_lower = [a.lower() for a in accepted]
                match_idx = next(
                    (j for j, v in enumerate(row_values) if v in accepted_lower),
                    None,
                )
                if match_idx is None:
                    all_found = False
                    break
                mapping[semantic_key] = str(match_idx)
            if all_found:
                return i, mapping
    preview_rows = df.head(10).values.tolist()
    raise ParserError(
        f"AMEX header row not detected in first {max_rows} rows. "
        f"None of KNOWN_COLUMN_SETS matched. "
        f"First 10 rows preview: {preview_rows}. "
        f"Add a new entry to KNOWN_COLUMN_SETS based on the actual headers."
    )


def _parse_date(s: Any) -> date:
    """AMEX India MyStatement: US `MM/DD/YYYY` format (verified Task 0 §0.1).

    AMEX preserves the merchant-network-side US date convention even on
    Indian-issued cards. Try US format FIRST. Falls back to ISO + DD-MM-YYYY
    for tolerance. DO NOT use `dayfirst=True` — that would mis-parse
    ambiguous dates (e.g. '03/05/2026' as May 3 instead of March 5).
    """
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ParserError(f"Could not parse date string: {s!r}")


def _row_from_signed_amount(date_str: str, description: str,
                            amount_value: Any, ordinal: int) -> ParsedRow:
    """AMEX signed-amount convention: positive = charge (out), negative = credit (in)."""
    val = Decimal(str(amount_value))
    return ParsedRow(
        txn_date=_parse_date(date_str),
        amount=abs(val),
        direction="out" if val > 0 else "in",
        raw_merchant=str(description).strip(),
        source_row_ordinal=ordinal,
    )


def _row_from_split(date_str: str, description: str, debit_value: Any,
                    credit_value: Any, ordinal: int) -> ParsedRow:
    """Split debit/credit columns — exactly one should be non-NaN+nonzero."""
    if pd.notna(debit_value) and Decimal(str(debit_value)) != 0:
        return ParsedRow(
            txn_date=_parse_date(date_str),
            amount=abs(Decimal(str(debit_value))),
            direction="out",
            raw_merchant=str(description).strip(),
            source_row_ordinal=ordinal,
        )
    if pd.notna(credit_value) and Decimal(str(credit_value)) != 0:
        return ParsedRow(
            txn_date=_parse_date(date_str),
            amount=abs(Decimal(str(credit_value))),
            direction="in",
            raw_merchant=str(description).strip(),
            source_row_ordinal=ordinal,
        )
    raise ParserError(
        f"Row at ordinal {ordinal} has no debit OR credit value; cannot determine direction"
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_total_row(df: pd.DataFrame, header_idx: int, desc_col_idx: int,
                    amount_col_idx: int) -> Decimal | None:
    """Search rows below the data band for a 'Total' / 'Statement Total' row.
    Returns the amount, or None if not found."""
    for i in range(header_idx + 1, len(df)):
        cell = df.iloc[i, desc_col_idx]
        if pd.isna(cell):
            continue
        if "total" in str(cell).strip().lower():
            amt = df.iloc[i, amount_col_idx]
            if pd.notna(amt):
                try:
                    return abs(Decimal(str(amt)))
                except Exception:  # noqa: BLE001
                    continue
    return None


def parse(xlsx_path: Path, password: str | None = None) -> ParseResult:
    """Read AMEX XLSX, extract rows + declared totals.

    `password` parameter is accepted for parser-interface uniformity but
    ignored — AMEX XLSX is unencrypted in V1.
    """
    xlsx_path = Path(xlsx_path)
    pdf_content_hash = _sha256_file(xlsx_path)

    df = pd.read_excel(xlsx_path, engine="openpyxl", header=None)
    # AMEX MyStatement exports include a trailing fully-empty column
    # (XLSX export artifact). Drop it before further processing — verified
    # Task 0 §0.1.
    df = df.dropna(axis=1, how="all")
    header_idx, mapping = find_header_row(df)

    rows: list[ParsedRow] = []
    ordinal = 1
    desc_col_idx = int(mapping["description"])
    date_col_idx = int(mapping["date"])

    if "amount" in mapping:
        amount_col_idx = int(mapping["amount"])
        for i in range(header_idx + 1, len(df)):
            row = df.iloc[i]
            d = row.iloc[date_col_idx]
            desc = row.iloc[desc_col_idx]
            amt = row.iloc[amount_col_idx]
            if pd.isna(d) or pd.isna(desc) or pd.isna(amt):
                continue
            if "total" in str(desc).strip().lower():
                continue
            try:
                rows.append(_row_from_signed_amount(str(d), str(desc), amt, ordinal))
                ordinal += 1
            except ParserError:
                logger.warning("skipping unparseable row at index %d: %r", i, row.to_list())
    else:
        # Split debit/credit layout
        debit_col_idx = int(mapping["debit"])
        credit_col_idx = int(mapping["credit"])
        for i in range(header_idx + 1, len(df)):
            row = df.iloc[i]
            d = row.iloc[date_col_idx]
            desc = row.iloc[desc_col_idx]
            debit = row.iloc[debit_col_idx]
            credit = row.iloc[credit_col_idx]
            if pd.isna(d) or pd.isna(desc):
                continue
            if pd.isna(debit) and pd.isna(credit):
                continue
            if "total" in str(desc).strip().lower():
                continue
            try:
                rows.append(_row_from_split(str(d), str(desc), debit, credit, ordinal))
                ordinal += 1
            except ParserError:
                logger.warning("skipping unparseable row at index %d: %r", i, row.to_list())

    # Declared totals
    declared_total_spends: Decimal | None = None
    if "amount" in mapping:
        declared_total_spends = _find_total_row(
            df, header_idx, desc_col_idx, int(mapping["amount"]),
        )

    derived_from_rows: bool
    if declared_total_spends is not None:
        derived_from_rows = False
        declared_totals: dict = {
            "total_spends": declared_total_spends,
            "total_credits": Decimal("0"),
            "closing_balance": None,
            "_derived_from_rows": derived_from_rows,
        }
    else:
        derived_from_rows = True
        logger.warning(
            "AMEX XLSX has no 'Total' row; deriving declared totals from row sums "
            "(validator will pass tautologically; success message annotates this)"
        )
        declared_totals = {
            "total_spends": sum((r.amount for r in rows if r.direction == "out"), Decimal("0")),
            "total_credits": sum((r.amount for r in rows if r.direction == "in"), Decimal("0")),
            "closing_balance": None,
            "_derived_from_rows": derived_from_rows,
        }

    return ParseResult(
        rows=rows,
        declared_totals=declared_totals,
        pdf_content_hash=pdf_content_hash,
        parser_version=__parser_version__,
    )
