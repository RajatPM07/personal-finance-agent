"""W5.1 refund + self-transfer detector.

Per docs/superpowers/specs/2026-05-21-refund-detection-design.md.

Pure heuristic detection (no LLM). Populates is_refund, is_self_transfer,
linked_txn_id on transactions rows after ingestion. Invoked from
pipeline.py via adb() so the synchronous DB calls don't block the async loop.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

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


_SELF_TRANSFER_WINDOW_DAYS = 2


class _Pending:
    """Sentinel — pattern matched but no cross-account debit found yet.
    Caller leaves is_self_transfer=NULL and waits for next detection run."""
    __slots__ = ()
    def __repr__(self) -> str:
        return "PENDING"


PENDING: _Pending = _Pending()


def _find_self_transfer_match(
    credit: Any,
    recent_debits: list[Any],
    patterns: list[str],
) -> Any | _Pending | None:
    """Return the matching cross-account debit row, PENDING (pattern hit but
    no cross-account match yet), or None (no pattern hit — proceed to refund).

    Per spec §5.2 step 1 + D3 + D4 + D6: pattern hit is REQUIRED; cross-account
    match (different account_id, exact amount, ±2 days, is_self_transfer IS NOT
    true) is sufficient when present. Multi-match: smallest date delta wins;
    tie-break smallest amount delta (amount is exact so rarely fires).

    Callers MUST distinguish the three return states by IDENTITY:
        match = _find_self_transfer_match(credit, debits, patterns)
        if match is PENDING:
            rows_pending += 1
        elif match is not None:
            link_cc_credit_to_savings_debit(credit, match)
        else:  # match is None — no pattern hit
            proceed_to_refund_matcher(credit)

    Do NOT use truthy checks. `bool(PENDING)` is True by default (intentional,
    to surface the distinction); a naïve `if match: ...` would treat PENDING
    as a successful match and write an incorrect link.
    """
    if not _matches_self_transfer(_row_get(credit, "raw_merchant"), patterns):
        return None

    credit_account = _row_get(credit, "account_id")
    credit_amount = _row_get(credit, "amount")
    credit_date = _row_get(credit, "date")

    best: Any | None = None
    best_delta: int | None = None
    for d in recent_debits:
        if _row_get(d, "account_id") == credit_account:
            continue
        if _row_get(d, "amount") != credit_amount:
            continue
        d_date = _row_get(d, "date")
        delta = abs((credit_date - d_date).days)
        if delta > _SELF_TRANSFER_WINDOW_DAYS:
            continue
        if _row_get(d, "is_self_transfer") is True:
            continue
        if best is None or (best_delta is not None and delta < best_delta):
            best = d
            best_delta = delta
    if best is not None:
        return best
    return PENDING


@dataclass(frozen=True)
class DetectionResult:
    refunds_linked: int = 0
    self_transfers_linked: int = 0
    rows_processed: int = 0
    rows_pending: int = 0


_PATTERNS_CACHE: dict[UUID, list[str]] | None = None


def _get_patterns() -> dict[UUID, list[str]]:
    global _PATTERNS_CACHE
    if _PATTERNS_CACHE is None:
        _PATTERNS_CACHE = _load_patterns()
    return _PATTERNS_CACHE


def detect_for_account(
    account_id: UUID,
    since: date | None = None,
    _conn_for_test: Any = None,
) -> DetectionResult:
    """Detect refunds + self-transfers for rows on this account.

    Two phases per spec §5.2 + §5.3:
      A. Process new direction='in' rows on this account.
      B. New direction='out' rows on this account may unblock pending
         pattern-credits on OTHER accounts.

    `_conn_for_test`: when provided (test mode), uses this psycopg connection
    for both reads and writes (so transaction-rollback fixture works).
    In production: SELECTs via readonly_client(), UPDATEs via service_client().
    """
    patterns = _get_patterns()

    if _conn_for_test is not None:
        return _detect_with_conn(account_id, since, _conn_for_test, patterns)
    else:
        from skills.finance.lib.db import readonly_client, service_client
        return _detect_production(account_id, since, patterns,
                                   readonly_client(), service_client())


def _detect_with_conn(
    account_id: UUID,
    since: date | None,
    conn: Any,
    patterns: dict[UUID, list[str]],
) -> DetectionResult:
    """Test-mode detection using a single psycopg connection for both reads
    and writes (transaction-rollback fixture relies on a single conn)."""
    return _detect_impl(
        account_id, since, patterns,
        read=lambda sql, params: _exec_fetch(conn, sql, params),
        write=lambda sql, params: _exec(conn, sql, params),
    )


def _detect_production(
    account_id: UUID,
    since: date | None,
    patterns: dict[UUID, list[str]],
    readonly_conn: Any,
    service_client_: Any,
) -> DetectionResult:
    """Production-mode detection: psycopg readonly for SELECT (bypasses
    Supabase 1000-row cap per spec §5.5), service client for UPDATE writes.

    The write adapter parses a fixed set of internal UPDATE shapes and dispatches
    to supabase-py's .table().update().eq() API. Only 4 SQL shapes are produced
    by _detect_impl; the regex parser refuses unknown shapes (fail loud).
    """
    def _write(sql: str, params: tuple) -> None:
        m = re.match(r"UPDATE transactions SET (.+) WHERE id = %s$", sql)
        if not m:
            raise ValueError(f"Unsupported UPDATE shape: {sql!r}")
        set_clause = m.group(1)
        txn_id = params[-1]
        assignments: dict[str, Any] = {}
        placeholder_idx = 0
        for part in [p.strip() for p in set_clause.split(",")]:
            col, _sep, val = part.partition(" = ")
            if val == "true":
                assignments[col] = True
            elif val == "false":
                assignments[col] = False
            elif val == "%s":
                assignments[col] = str(params[placeholder_idx])
                placeholder_idx += 1
            else:
                raise ValueError(f"Unsupported SET value in {sql!r}: {part!r}")
        service_client_.table("transactions").update(assignments).eq("id", str(txn_id)).execute()

    return _detect_impl(
        account_id, since, patterns,
        read=lambda sql, params: _exec_fetch(readonly_conn, sql, params),
        write=_write,
    )


def _detect_impl(
    account_id: UUID,
    since: date | None,
    patterns: dict[UUID, list[str]],
    read: Any,
    write: Any,
) -> DetectionResult:
    """The detection algorithm, parameterised on read/write callables so the
    same logic runs in test mode (single psycopg conn, transaction-rollback)
    and production mode (split read/write)."""
    refunds_linked = 0
    self_transfers_linked = 0
    rows_processed = 0
    rows_pending = 0

    # --- Phase A: new credits on this account ------------------------------
    since_clause = "AND date >= %s" if since is not None else ""
    since_params: tuple = (since,) if since is not None else ()
    credits = read(
        f"""
        SELECT id, account_id, date, amount, direction, raw_merchant,
               is_refund, is_self_transfer, linked_txn_id
        FROM transactions
        WHERE account_id = %s AND direction = 'in'
          AND (is_refund IS NULL OR is_self_transfer IS NULL)
          {since_clause}
        ORDER BY date
        """,
        (account_id,) + since_params,
    )

    acct_patterns = patterns.get(account_id, [])
    for credit in credits:
        # Respect already-set flags (user overrides or prior detection runs).
        # The outer SELECT picks up rows where EITHER flag is NULL — but a row
        # may have `is_refund=true` set by the user with `is_self_transfer=NULL`
        # still pending; we must skip the refund check on that row.
        already_refund = credit["is_refund"] is not None
        already_self_transfer = credit["is_self_transfer"] is not None

        st_match: Any | _Pending | None = None
        if acct_patterns and not already_self_transfer and not already_refund:
            debit_lookup_since = credit["date"] - timedelta(days=_SELF_TRANSFER_WINDOW_DAYS)
            debit_lookup_until = credit["date"] + timedelta(days=_SELF_TRANSFER_WINDOW_DAYS)
            cross_debits = read(
                """
                SELECT id, account_id, date, amount, direction, raw_merchant,
                       is_refund, is_self_transfer
                FROM transactions
                WHERE direction = 'out'
                  AND account_id != %s
                  AND amount = %s
                  AND date BETWEEN %s AND %s
                  AND (is_self_transfer IS NULL OR is_self_transfer = false)
                """,
                (account_id, credit["amount"], debit_lookup_since, debit_lookup_until),
            )
            st_match = _find_self_transfer_match(credit, cross_debits, acct_patterns)

        if st_match is PENDING:
            rows_pending += 1
            continue
        if st_match is not None and st_match is not PENDING:
            # st_match is a row dict at this point; narrow for mypy.
            st_row: Any = st_match
            write(
                "UPDATE transactions SET is_self_transfer = true, linked_txn_id = %s WHERE id = %s",
                (st_row["id"], credit["id"]),
            )
            write(
                "UPDATE transactions SET is_self_transfer = true WHERE id = %s",
                (st_row["id"],),
            )
            self_transfers_linked += 1
            rows_processed += 1
            continue

        # Refund check — skip if user already set is_refund or row was
        # previously flagged as a self-transfer.
        if already_refund or already_self_transfer:
            continue

        debit_lookup_since = credit["date"] - timedelta(days=_REFUND_WINDOW_DAYS)
        debit_lookup_until = credit["date"] - timedelta(days=1)
        refund_candidates = read(
            """
            SELECT id, account_id, date, amount, direction, raw_merchant,
                   is_refund, is_self_transfer
            FROM transactions
            WHERE account_id = %s AND direction = 'out'
              AND amount = %s
              AND date BETWEEN %s AND %s
              AND (is_refund IS NULL OR is_refund = false)
              AND (is_self_transfer IS NULL OR is_self_transfer = false)
            """,
            (account_id, credit["amount"], debit_lookup_since, debit_lookup_until),
        )
        refund_match = _find_refund_match(credit, refund_candidates)
        if refund_match is not None:
            write(
                "UPDATE transactions SET is_refund = true, linked_txn_id = %s WHERE id = %s",
                (refund_match["id"], credit["id"]),
            )
            refunds_linked += 1
            rows_processed += 1
        else:
            # No match either way: mark processed (per 3.i)
            write(
                "UPDATE transactions SET is_refund = false, is_self_transfer = false WHERE id = %s",
                (credit["id"],),
            )
            rows_processed += 1

    # --- Phase B: new debits on this account may unblock pending elsewhere -
    new_debits = read(
        f"""
        SELECT id, account_id, date, amount FROM transactions
        WHERE account_id = %s AND direction = 'out'
          {since_clause}
        """,
        (account_id,) + since_params,
    )
    for debit in new_debits:
        d_window_start = debit["date"] - timedelta(days=_SELF_TRANSFER_WINDOW_DAYS)
        d_window_end = debit["date"] + timedelta(days=_SELF_TRANSFER_WINDOW_DAYS)
        pending_credits = read(
            """
            SELECT id, account_id, date, amount, raw_merchant
            FROM transactions
            WHERE direction = 'in'
              AND account_id != %s
              AND amount = %s
              AND date BETWEEN %s AND %s
              AND is_self_transfer IS NULL
            """,
            (account_id, debit["amount"], d_window_start, d_window_end),
        )
        for cand in pending_credits:
            cand_patterns = patterns.get(cand["account_id"], [])
            if _matches_self_transfer(cand["raw_merchant"], cand_patterns):
                write(
                    "UPDATE transactions SET is_self_transfer = true, linked_txn_id = %s WHERE id = %s",
                    (debit["id"], cand["id"]),
                )
                write(
                    "UPDATE transactions SET is_self_transfer = true WHERE id = %s",
                    (debit["id"],),
                )
                self_transfers_linked += 1
                rows_processed += 1
                break  # one debit unblocks at most one credit

    return DetectionResult(
        refunds_linked=refunds_linked,
        self_transfers_linked=self_transfers_linked,
        rows_processed=rows_processed,
        rows_pending=rows_pending,
    )


# --- minimal psycopg helpers ------------------------------------------------

def _exec_fetch(conn: Any, sql: str, params: tuple) -> list[dict]:
    """Execute SELECT, return list of dicts."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        cols = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def _exec(conn: Any, sql: str, params: tuple) -> None:
    """Execute non-returning statement."""
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _exec_fetch_psycopg(conn: Any, sql: str, params: tuple) -> list[dict]:
    """Alias kept for clarity in the production split."""
    return _exec_fetch(conn, sql, params)
