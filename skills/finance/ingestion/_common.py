"""Shared dataclasses, constants, and pure helpers for the ingestion pipeline.

No I/O outside `password_lookup` (which reads credentials.yaml). The dataclasses
live here so parsers, validator, pipeline, watcher, and doc handler all import
from a single source of truth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml

# V1 single-user. Hardcoded UUID matching the row seeded by 003_seed.local.sql.
# V2 onboarding will move this to settings.py when Ayushi is added.
RAJAT_USER_ID: str = "00000000-0000-0000-0000-000000000001"

Bank = Literal["icici_cc", "amex_cc", "paytm_upi"]


class AmbiguousCredentialError(Exception):
    """Raised when password_lookup matches multiple credentials.yaml keys
    without a last4 disambiguator. Caller must supply last4."""


class CredentialNotFoundError(Exception):
    """Raised when password_lookup finds no matching key in credentials.yaml."""


@dataclass(frozen=True)
class ParsedRow:
    txn_date: date
    amount: Decimal                       # always positive; sign info on `direction`
    direction: Literal["in", "out"]
    raw_merchant: str
    source_row_ordinal: int                # 1..N within the file, deterministic per parser
    # Paytm-only fields (W3.1). ICICI/AMEX rows leave these at defaults.
    is_amex_routed: bool = False           # True → row is dropped at insert (D1 dual-entry skip)
    is_self_transfer: bool = False         # True → Paytm 'Money sent to ...' row to a known own-handle (D2)
    category_hint: str | None = None       # Paytm's pre-tagged category, emoji-stripped (D4)


@dataclass(frozen=True)
class ParseResult:
    rows: list[ParsedRow]
    declared_totals: dict                  # {'total_spends': Decimal, 'total_credits': Decimal,
                                           #  'closing_balance': Decimal | None,
                                           #  '_derived_from_rows': bool}
    pdf_content_hash: str                  # sha256 of source FILE bytes (PDF or XLSX);
                                           #   threaded into import_hash. Field name kept as
                                           #   pdf_content_hash for schema compatibility with
                                           #   transactions table; despite the name, also used
                                           #   for XLSX content hashing.
    parser_version: str                    # e.g. "icici-cc/v1"

    def insertable_rows(self) -> list[ParsedRow]:
        """Rows the pipeline should persist. Excludes is_amex_routed=True rows
        (D1: same spend already captured via the AMEX statement). Self-transfers
        ARE in the insertable set (D2: ingest all transactions even when
        excluded from the published-summary count)."""
        return [r for r in self.rows if not r.is_amex_routed]


@dataclass(frozen=True)
class SourceMeta:
    source: Literal["manual_pdf", "manual_xlsx", "telegram_pdf", "telegram_xlsx", "gmail_cc_stmt"]
    source_ref: str                        # filename / message_id / email_id


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    delta_in: Decimal
    delta_out: Decimal
    rows_count: int
    declared_in: Decimal
    declared_out: Decimal
    extracted_in: Decimal
    extracted_out: Decimal


def detect_bank_from_filename(filename: str) -> Bank | None:
    """Pure function. Lowercase + word-boundary token match.

    Tokenizes the filename on non-alphanumerics so 'cc' must appear as a
    standalone token (NOT as a substring of words like 'account' which
    contains 'cc' as a bigram).

    Returns 'icici_cc' if filename has 'icici' AND 'cc' tokens.
    Returns 'amex_cc' if filename has 'amex' OR 'american' tokens.
    Returns None if both ICICI and AMEX tokens appear (ambiguous), or if
    neither set matches.
    """
    name = filename.lower()
    tokens = set(re.split(r"[^a-z0-9]+", name))
    has_icici = "icici" in tokens
    has_amex = ("amex" in tokens) or ("american" in tokens)
    if has_icici and has_amex:
        return None
    if has_icici and ("cc" in tokens):
        return "icici_cc"
    if has_amex:
        return "amex_cc"
    return None


def password_lookup(bank: Bank, last4: str | None = None,
                    credentials_path: Path = Path("credentials.yaml")) -> str:
    """Read credentials.yaml. Returns the password for the given bank.

    NB: AMEX in V1 is XLSX without a password — callers should not invoke this
    helper for `bank='amex_cc'`. ICICI is the only V1 caller. The function still
    accepts the bank parameter for forward-compatibility with future
    password-protected sources (e.g. when HDFC CC is added later, extend the
    `Bank` literal here and add `hdfc_cc_<last4>` keys to credentials.yaml).
    """
    with open(credentials_path) as f:
        creds: dict = yaml.safe_load(f) or {}

    def _extract_password(entry: dict, key: str) -> str:
        """Pull the 'value' field as a non-empty string, or raise CredentialNotFoundError."""
        if not isinstance(entry, dict):
            raise CredentialNotFoundError(
                f"Credential entry '{key}' in {credentials_path} is not a mapping"
            )
        pw = entry.get("value")
        if not isinstance(pw, str) or not pw:
            raise CredentialNotFoundError(
                f"Credential entry '{key}' in {credentials_path} has no usable "
                f"'value' field (got: {pw!r}). Fill it in and retry."
            )
        return pw

    if last4:
        key = f"{bank}_{last4}"
        if key not in creds:
            raise CredentialNotFoundError(
                f"No credential entry for '{key}' in {credentials_path}"
            )
        return _extract_password(creds[key], key)

    matching = [k for k in creds if k.startswith(f"{bank}_")]
    if not matching:
        raise CredentialNotFoundError(
            f"No credential entries with prefix '{bank}_' in {credentials_path}"
        )
    if len(matching) > 1:
        raise AmbiguousCredentialError(
            f"Multiple credential entries match '{bank}_*': {matching}. "
            f"Pass last4 to disambiguate."
        )
    return _extract_password(creds[matching[0]], matching[0])
