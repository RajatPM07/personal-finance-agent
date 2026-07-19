"""Deterministic canonicalizer for ICICI credit-card raw_merchant strings.

ICICI CC statement rows arrive (from ``icici_cc.py``'s ``ROW_RE``, which only
peels the trailing INR decimal off as ``amount``) shaped as::

    <11-digit txn ref> <MERCHANT + city/country> <reward-points int> [<fx amount>]

e.g. ``13483665323 AMAZON PAY IN E COMMERC BANGALORE 242``. The per-transaction
ref and the reward-points column differ on every row, so 24 identical Amazon
purchases look like 24 distinct merchants. Downstream categorization then sees
one merchant as N — wasting LLM calls and producing *inconsistent* labels
across batches (the same string categorized Shopping on some rows, Needs Review
on others).

``normalize_merchant`` strips the ref + points (and any foreign-currency
amount) to a stable canonical key.

SAFETY: the regex is anchored to the FULL ICICI-CC signature — a 10-12 digit
leading ref AND a trailing signed integer. Any string that does not match that
exact shape (UPI handles, person names, AMEX / Paytm / PhonePe / ICICI-Savings
descriptors) is returned UNCHANGED. Validated against the whole transactions
table: 0 non-``icici-cc/v1`` rows are altered. This is a read-side helper only —
it does NOT feed ``normalized_description`` / ``import_hash``, so using it needs
no ``__parser_version__`` bump (CLAUDE.md invariant #4). Ingestion-side merchant
normalization (pipeline.py's Week-2 TODO) is a separate, parser-bumping change.
"""
from __future__ import annotations

import re
from typing import overload

# ^<ref 10-12 digits> <body (non-greedy)> <points int> [optional trailing fx amount]$
_ICICI_CC_RE = re.compile(
    r"^(?P<ref>\d{10,12})\s+"
    r"(?P<body>.+?)"
    r"\s+(?P<pts>-?\d+)"
    r"(?:\s+\d{1,3}(?:,\d{3})*\.\d{2})?$"
)


@overload
def normalize_merchant(raw: str) -> str: ...
@overload
def normalize_merchant(raw: None) -> None: ...
def normalize_merchant(raw: str | None) -> str | None:
    """Canonical merchant key.

    For ICICI-CC strings, strip the leading txn ref and trailing reward-points
    (and any foreign-currency amount) and collapse internal whitespace. For
    every other string — anything not matching the anchored ICICI-CC signature —
    return it unchanged (strict no-op). ``None``/empty pass through unchanged.
    """
    if not raw:
        return raw
    m = _ICICI_CC_RE.match(raw.strip())
    if not m:
        return raw
    body = re.sub(r"\s+", " ", m.group("body")).strip()
    return body or raw
