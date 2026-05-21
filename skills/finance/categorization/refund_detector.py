"""W5.1 refund + self-transfer detector.

Per docs/superpowers/specs/2026-05-21-refund-detection-design.md.

Pure heuristic detection (no LLM). Populates is_refund, is_self_transfer,
linked_txn_id on transactions rows after ingestion. Invoked from
pipeline.py via adb() so the synchronous DB calls don't block the async loop.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from rapidfuzz import fuzz

_PATTERNS_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "self_transfer_patterns.yaml"
)

_FUZZ_THRESHOLD = 80
_REFUND_WINDOW_DAYS = 30


def _load_patterns(path: Path | None = None) -> dict[UUID, list[str]]:
    """Load per-account self-transfer text patterns from yaml.

    Fail loud at load time (per spec §7) — empty list, empty string, malformed
    YAML, or missing file all raise rather than silently returning empty
    behavior. The error category that would otherwise hide is "every row
    processed as not-a-self-transfer."
    """
    p = path if path is not None else _PATTERNS_PATH
    with open(p) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected a mapping, got {type(raw).__name__}")

    out: dict[UUID, list[str]] = {}
    for key, patterns in raw.items():
        try:
            acct = UUID(str(key))
        except ValueError as e:
            raise ValueError(f"{p}: key {key!r} is not a valid UUID") from e
        if not isinstance(patterns, list):
            raise ValueError(f"{p}[{key}]: expected a list, got {type(patterns).__name__}")
        if len(patterns) == 0:
            raise ValueError(
                f"{p}[{key}]: empty patterns list — refuse silent "
                "always-False behavior. Remove the key or add at least one pattern."
            )
        for s in patterns:
            if not isinstance(s, str) or not s.strip():
                raise ValueError(
                    f"{p}[{key}]: empty or non-string pattern {s!r} — "
                    "would match every row."
                )
        out[acct] = list(patterns)
    if len(out) == 0:
        raise ValueError(
            f"{p}: no account patterns found — empty config would silently process "
            "every row as not-a-self-transfer, the exact §7 hazard this loader exists to prevent."
        )
    return out


def _matches_self_transfer(raw_merchant: str | None, patterns: list[str]) -> bool:
    """Case-insensitive substring match, multi-pattern OR."""
    if not raw_merchant:
        return False
    haystack = raw_merchant.casefold()
    return any(p.casefold() in haystack for p in patterns)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Tolerant attr-or-subscript access — _find_refund_match accepts both
    dict-like rows (from psycopg) and dataclass-like rows (from tests)."""
    if hasattr(row, key):
        return getattr(row, key)
    if hasattr(row, "__getitem__"):
        try:
            return row[key]
        except (KeyError, TypeError):
            pass
    return default


def _find_refund_match(credit: Any, candidates: list[Any]) -> Any | None:
    """Pick the best refund original for `credit` from `candidates`.

    Returns the chosen candidate row (or None if none qualify).
    Per spec §5.2 step 2 + D2 + D6: exact amount, same account, fuzzy merchant
    >= 80, window [credit.date - 30d, credit.date - 1d], exclude candidates
    already flagged as refund or self-transfer. On ties, smallest date delta wins.
    """
    credit_merchant = _row_get(credit, "raw_merchant")
    if not credit_merchant:
        return None
    credit_date = _row_get(credit, "date")
    credit_amount = _row_get(credit, "amount")
    credit_account = _row_get(credit, "account_id")

    earliest_allowed = credit_date - timedelta(days=_REFUND_WINDOW_DAYS)
    latest_allowed = credit_date - timedelta(days=1)

    best: Any | None = None
    best_delta: int | None = None
    for c in candidates:
        if _row_get(c, "account_id") != credit_account:
            continue
        if _row_get(c, "amount") != credit_amount:
            continue
        c_date = _row_get(c, "date")
        if c_date < earliest_allowed or c_date > latest_allowed:
            continue
        if _row_get(c, "is_refund") is True:
            continue
        if _row_get(c, "is_self_transfer") is True:
            continue
        c_merchant = _row_get(c, "raw_merchant")
        if not c_merchant:
            continue
        score = fuzz.token_set_ratio(credit_merchant, c_merchant)
        if score < _FUZZ_THRESHOLD:
            continue
        delta = (credit_date - c_date).days
        if best is None or (best_delta is not None and delta < best_delta):
            best = c
            best_delta = delta
    return best
