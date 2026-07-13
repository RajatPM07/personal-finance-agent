"""PhonePe UPI transaction-statement PDF parser — deterministic.

PhonePe exports a password-protected PDF (password = the account phone number).
Records span 4 text lines:

    <Mon DD, YYYY> (Paid to|Received from|Paid -) <details> (Debit|Credit) INR <amount>
    <HH:MM AM/PM> Transaction ID : T........  [<amount, when it overflowed line 1>]
    UTR No : ............
    Debited from <acct> | Credited to <acct>

For most rows the amount is on line 1. For large transfers pdfplumber pushes the
amount onto line 2 (after the Transaction ID) — we recover it there. Debit→out,
Credit→in. PhonePe is Ayushi's UPI source of truth (she has no Paytm), so no
UPI-skip rule applies here.
"""
from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pikepdf

from skills.finance.ingestion._common import ParsedRow, ParseResult

logger = logging.getLogger(__name__)

__parser_version__ = "phonepe-upi/v1"

# Line 1: date + "Paid to X"/"Received from Y"/"Paid - Z" + Debit/Credit + INR + optional amount
_DETAIL_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2}) (?P<day>\d{2}), (?P<year>\d{4})\s+"
    r"(?P<details>.+?)\s+"
    r"(?P<type>Debit|Credit)\s+INR\s*(?P<amount>[\d,]+\.\d{2})?\s*$"
)
# Line 2: time + txn id, optionally trailing overflow amount
_TXN_RE = re.compile(
    r"^\d{2}:\d{2}\s+[AP]M\s+Transaction ID\s*:\s*(?P<txnid>\S+)"
    r"(?:\s+(?P<amount>[\d,]+\.\d{2}))?\s*$"
)

_MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1)}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _amount(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def parse(pdf_path: Path, password: str) -> ParseResult:
    pdf_path = Path(pdf_path)
    file_hash = _sha256_file(pdf_path)

    lines: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        with pikepdf.open(pdf_path, password=password) as src:
            src.save(tmp.name)
        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                lines.extend((page.extract_text() or "").splitlines())

    rows: list[ParsedRow] = []
    total_out = Decimal(0)
    total_in = Decimal(0)
    ordinal = 0
    i = 0
    n = len(lines)
    while i < n:
        m = _DETAIL_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        amount_str = m.group("amount")
        # Recover overflow amount from the Transaction-ID line if line 1 had none.
        if amount_str is None and i + 1 < n:
            tm = _TXN_RE.match(lines[i + 1].strip())
            if tm and tm.group("amount"):
                amount_str = tm.group("amount")
        if amount_str is None:
            logger.warning("phonepe: record with no recoverable amount at line %d: %r", i, lines[i][:80])
            i += 1
            continue
        amount = _amount(amount_str)
        direction = "out" if m.group("type") == "Debit" else "in"
        txn_date = date(int(m.group("year")), _MONTHS[m.group("mon")], int(m.group("day")))

        ordinal += 1
        rows.append(ParsedRow(
            txn_date=txn_date,
            amount=amount,
            direction=direction,  # type: ignore[arg-type]
            raw_merchant=m.group("details").strip(),
            source_row_ordinal=ordinal,
        ))
        if direction == "out":
            total_out += amount
        else:
            total_in += amount
        i += 1

    logger.info("phonepe: parsed %d rows from %s", len(rows), pdf_path.name)
    declared_totals = {
        "total_spends": total_out,
        "total_credits": total_in,
        "closing_balance": None,
        "_derived_from_rows": True,
    }
    return ParseResult(
        rows=rows,
        declared_totals=declared_totals,
        pdf_content_hash=file_hash,
        parser_version=__parser_version__,
    )
