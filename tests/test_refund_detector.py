"""W5.1 refund detector — matcher units + detect_for_account integration.

Per docs/superpowers/specs/2026-05-21-refund-detection-design.md §5 + §8.

Unit half (this file): pure functions, no DB. Mocked-row dataclass-like
dicts as input. Run on every CI.

Integration half (appended in Task 5): live DB, gated like
tests/test_readonly_client.py, transaction-rollback fixture so seeded rows
never persist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from skills.finance.categorization.refund_detector import _find_refund_match

AMEX_CC = UUID("10000000-0000-0000-0000-000000000005")
ICICI_CC = UUID("10000000-0000-0000-0000-000000000003")


@dataclass
class FakeRow:
    """Minimal stand-in for a transactions row, with just the fields
    _find_refund_match inspects. Real impl will be passed dicts coming
    from psycopg's cursor; the matcher should work with attribute OR
    subscript access — implementation choice."""
    id: UUID
    account_id: UUID
    date: date
    amount: Decimal
    direction: str
    raw_merchant: str | None
    is_refund: bool | None = None
    is_self_transfer: bool | None = None


def _credit(date_=None, merchant="Amazon", amount="500.00", acct=AMEX_CC):
    return FakeRow(
        id=uuid4(), account_id=acct,
        date=date_ or date(2026, 3, 15),
        amount=Decimal(amount), direction="in",
        raw_merchant=merchant,
    )


def _debit(date_, merchant="Amazon", amount="500.00", acct=AMEX_CC,
           is_refund=None, is_self_transfer=None):
    return FakeRow(
        id=uuid4(), account_id=acct,
        date=date_, amount=Decimal(amount),
        direction="out", raw_merchant=merchant,
        is_refund=is_refund, is_self_transfer=is_self_transfer,
    )


def test_single_exact_candidate_matches():
    credit = _credit()
    candidates = [_debit(date(2026, 3, 1), "Amazon")]
    match = _find_refund_match(credit, candidates)
    assert match is not None
    assert match.id == candidates[0].id


def test_multiple_candidates_pick_smallest_date_delta():
    """D6 locked: chronologically closest wins."""
    credit = _credit(date_=date(2026, 3, 15))
    older = _debit(date(2026, 2, 20), "Amazon")
    newer = _debit(date(2026, 3, 8), "Amazon")
    match = _find_refund_match(credit, [older, newer])
    assert match.id == newer.id


def test_rapidfuzz_score_79_rejected():
    """Threshold is >= 80 (D2)."""
    credit = _credit(merchant="Amazon")
    # Construct a merchant that scores below 80
    candidates = [_debit(date(2026, 3, 1), "Swiggy Bangalore Restaurant Delivery")]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_rapidfuzz_score_80_accepted_via_variant():
    """Real-world variant: 'Amazon' vs 'Amazon Mumbai' should pass token_set_ratio
    threshold of 80 (verified in Task 0.1)."""
    credit = _credit(merchant="Amazon")
    candidates = [_debit(date(2026, 3, 1), "Amazon Mumbai")]
    match = _find_refund_match(credit, candidates)
    assert match is not None


def test_same_day_candidate_excluded():
    """Window is [credit.date - 30d, credit.date - 1d] — same-day excluded
    (typically adjustment artifacts, not refunds)."""
    credit = _credit(date_=date(2026, 3, 15))
    candidates = [_debit(date(2026, 3, 15), "Amazon")]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_thirty_one_day_old_candidate_excluded():
    credit = _credit(date_=date(2026, 3, 31))
    candidates = [_debit(date(2026, 2, 28), "Amazon")]  # 31 days back
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_amount_mismatch_excluded():
    credit = _credit(amount="500.00")
    candidates = [_debit(date(2026, 3, 1), "Amazon", amount="501.00")]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_null_raw_merchant_on_credit_returns_none():
    credit = _credit(merchant=None)
    candidates = [_debit(date(2026, 3, 1), "Amazon")]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_null_raw_merchant_on_candidate_excluded():
    credit = _credit(merchant="Amazon")
    candidates = [_debit(date(2026, 3, 1), None)]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_candidate_with_is_refund_true_excluded():
    """Cycle prevention — don't link a refund to another refund (§7)."""
    credit = _credit()
    candidates = [_debit(date(2026, 3, 1), "Amazon", is_refund=True)]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_candidate_with_is_self_transfer_true_excluded():
    """Cross-linking prevention — don't link a refund into a self-transfer
    pair (§7 cycle-detection scope sub-decision)."""
    credit = _credit()
    candidates = [_debit(date(2026, 3, 1), "Amazon", is_self_transfer=True)]
    match = _find_refund_match(credit, candidates)
    assert match is None


def test_different_account_excluded():
    """Refund matcher is same-account-only (D2)."""
    credit = _credit(acct=AMEX_CC)
    candidates = [_debit(date(2026, 3, 1), "Amazon", acct=ICICI_CC)]
    match = _find_refund_match(credit, candidates)
    assert match is None


# --- self-transfer matcher tests --------------------------------------------

from skills.finance.categorization.refund_detector import (  # noqa: E402
    PENDING,
    _find_self_transfer_match,
)


def test_self_transfer_pattern_hit_with_cross_account_match():
    """Pattern matches AND a cross-account debit with same amount within ±2d
    exists → returns the debit row."""
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     date_=date(2026, 3, 15), amount="60000.00")
    debit = _debit(date(2026, 3, 14), "ACH/AMEX BILL PAYMENT", amount="60000.00",
                   acct=UUID("10000000-0000-0000-0000-000000000001"))  # savings
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [debit], patterns)
    assert match is not None
    assert match is not PENDING
    assert match.id == debit.id


def test_self_transfer_pattern_hit_no_cross_account_returns_pending():
    """Pattern matches but no cross-account debit exists yet → returns PENDING
    sentinel so caller leaves is_self_transfer=NULL (waits for the savings
    statement to be ingested)."""
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     amount="60000.00")
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [], patterns)
    assert match is PENDING


def test_self_transfer_no_pattern_returns_none():
    """No pattern hit → returns None (caller proceeds to refund check)."""
    credit = _credit(merchant="Amazon Mumbai", acct=AMEX_CC)
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [], patterns)
    assert match is None


def test_self_transfer_only_same_account_debit_returns_pending():
    """Pattern hits but the only matching-amount debit is in the same
    account — cross-account is required. Treated as pending (wait for a
    cross-account debit to appear)."""
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     amount="60000.00")
    same_acct_debit = _debit(date(2026, 3, 14), "Something", amount="60000.00",
                              acct=AMEX_CC)
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [same_acct_debit], patterns)
    assert match is PENDING


def test_self_transfer_multiple_cross_account_smallest_date_delta_wins():
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     date_=date(2026, 3, 15), amount="60000.00")
    far_debit = _debit(date(2026, 3, 13), "X", amount="60000.00",  # 2d back
                       acct=UUID("10000000-0000-0000-0000-000000000001"))
    near_debit = _debit(date(2026, 3, 14), "Y", amount="60000.00",  # 1d back
                        acct=UUID("10000000-0000-0000-0000-000000000001"))
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [far_debit, near_debit], patterns)
    assert match.id == near_debit.id


def test_self_transfer_cross_account_with_is_self_transfer_true_excluded():
    """Already-flagged debits aren't re-linkable."""
    credit = _credit(merchant="PAYMENT RECEIVED. THANK YOU", acct=AMEX_CC,
                     amount="60000.00")
    debit = _debit(date(2026, 3, 14), "X", amount="60000.00",
                   acct=UUID("10000000-0000-0000-0000-000000000001"),
                   is_self_transfer=True)
    patterns = ["PAYMENT RECEIVED. THANK YOU"]
    match = _find_self_transfer_match(credit, [debit], patterns)
    assert match is PENDING


# --- detect_for_account integration tests (live DB, gated) ------------------

import psycopg  # noqa: E402
import pytest  # noqa: E402

from skills.finance.categorization.refund_detector import (  # noqa: E402
    detect_for_account,
)


def _readonly_password_available() -> bool:
    try:
        from skills.finance.lib.settings import settings
        return bool(settings.supabase_readonly_password)
    except Exception:
        return False


_LIVE = pytest.mark.skipif(
    not _readonly_password_available(),
    reason="SUPABASE_READONLY_PASSWORD not in settings — live integration tests skipped",
)

# Reusable account UUIDs from 003_seed.local.sql.
SAVINGS = UUID("10000000-0000-0000-0000-000000000001")


def _write_dsn() -> str:
    """Service-role psycopg DSN for INSERT/UPDATE — we use the SUPABASE_DB_URL
    directly (same DSN scripts/backup_supabase.py uses). All test writes are
    inside a transaction that's rolled back at teardown — seeded rows never
    persist."""
    from skills.finance.lib.settings import settings
    return settings.supabase_db_url


@pytest.fixture
def write_conn():
    """Open a transaction we'll roll back at teardown. Tests INSERT into
    transactions through this connection and assert via direct SELECTs;
    no row commits. Per spec §8.3 decision 6.i."""
    conn = psycopg.connect(_write_dsn(), autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _insert(conn, **fields) -> UUID:
    """Insert one transactions row with given fields, return the id."""
    fields.setdefault("id", uuid4())
    fields.setdefault("user_id", UUID("00000000-0000-0000-0000-000000000001"))  # Rajat
    fields.setdefault("currency", "INR")
    cols = ",".join(fields.keys())
    placeholders = ",".join(["%s"] * len(fields))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO transactions ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
    return fields["id"]


def _select_flags(conn, txn_id: UUID) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_refund, is_self_transfer, linked_txn_id FROM transactions WHERE id = %s",
            (txn_id,),
        )
        r = cur.fetchone()
        return {
            "is_refund": r[0], "is_self_transfer": r[1], "linked_txn_id": r[2],
        }


# Integration-test dates are pushed to year 2099 to isolate from the 1227
# pre-existing rows in the live DB. The `since` filter scopes Phase A reads;
# Phase B's debit window is keyed off the seeded debit's date, so the ±2d
# window also lands in 2099 — no live rows can drift into either query.

@_LIVE
def test_refund_happy_path(write_conn):
    """Seed: AMEX debit + matching credit. Run detect_for_account. Assert
    is_refund=true + linked_txn_id pointing to the debit."""
    debit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2099, 3, 1),
        amount=Decimal("500.00"), direction="out",
        raw_merchant="Amazon", is_refund=None,
    )
    credit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2099, 3, 15),
        amount=Decimal("500.00"), direction="in",
        raw_merchant="Amazon Mumbai", is_refund=None,
    )
    result = detect_for_account(
        AMEX_CC, since=date(2099, 2, 1),
        _conn_for_test=write_conn,
    )
    flags = _select_flags(write_conn, credit_id)
    assert flags["is_refund"] is True
    assert flags["linked_txn_id"] == debit_id
    assert result.refunds_linked == 1


@_LIVE
def test_self_transfer_happy_path(write_conn):
    """Seed: AMEX credit with PAYMENT RECEIVED + savings matching debit.
    Assert both flagged, linked_txn_id only on CC."""
    debit_id = _insert(
        write_conn, account_id=SAVINGS, date=date(2099, 3, 14),
        amount=Decimal("60000.00"), direction="out",
        raw_merchant="AMEX CC BILL PAYMENT", is_self_transfer=None,
    )
    credit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2099, 3, 15),
        amount=Decimal("60000.00"), direction="in",
        raw_merchant="PAYMENT RECEIVED. THANK YOU",
        is_self_transfer=None,
    )
    result = detect_for_account(
        AMEX_CC, since=date(2099, 2, 1),
        _conn_for_test=write_conn,
    )
    cc_flags = _select_flags(write_conn, credit_id)
    savings_flags = _select_flags(write_conn, debit_id)
    assert cc_flags["is_self_transfer"] is True
    assert cc_flags["linked_txn_id"] == debit_id
    assert savings_flags["is_self_transfer"] is True
    assert savings_flags["linked_txn_id"] is None  # one-way FK
    assert result.self_transfers_linked == 1


@_LIVE
def test_phase_b_catch_up(write_conn):
    """Ingest CC credit first (no savings yet) → leaves pending. Then ingest
    savings debit; running detect_for_account(savings) Phase B unblocks the
    CC credit."""
    credit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2099, 3, 15),
        amount=Decimal("60000.00"), direction="in",
        raw_merchant="PAYMENT RECEIVED. THANK YOU",
        is_self_transfer=None,
    )
    # First detect — no savings debit yet
    r1 = detect_for_account(AMEX_CC, since=date(2099, 2, 1), _conn_for_test=write_conn)
    cc_flags_1 = _select_flags(write_conn, credit_id)
    assert cc_flags_1["is_self_transfer"] is None  # still pending
    assert r1.rows_pending == 1
    # Now seed savings debit and run detection for savings
    debit_id = _insert(
        write_conn, account_id=SAVINGS, date=date(2099, 3, 14),
        amount=Decimal("60000.00"), direction="out",
        raw_merchant="AMEX BILL", is_self_transfer=None,
    )
    r2 = detect_for_account(SAVINGS, since=date(2099, 2, 1), _conn_for_test=write_conn)
    cc_flags_2 = _select_flags(write_conn, credit_id)
    savings_flags = _select_flags(write_conn, debit_id)
    assert cc_flags_2["is_self_transfer"] is True
    assert cc_flags_2["linked_txn_id"] == debit_id
    assert savings_flags["is_self_transfer"] is True
    assert r2.self_transfers_linked == 1


@_LIVE
def test_idempotent_re_run_is_noop(write_conn):
    """First run processes a row; second run is a no-op (skips via IS NULL guard)."""
    _insert(
        write_conn, account_id=AMEX_CC, date=date(2099, 3, 15),
        amount=Decimal("500.00"), direction="in",
        raw_merchant="Random Merchant", is_refund=None,
    )
    r1 = detect_for_account(AMEX_CC, since=date(2099, 2, 1), _conn_for_test=write_conn)
    r2 = detect_for_account(AMEX_CC, since=date(2099, 2, 1), _conn_for_test=write_conn)
    assert r1.rows_processed >= 1
    assert r2.rows_processed == 0


@_LIVE
def test_user_override_preserved(write_conn):
    """A row already flagged is_refund=true is left alone on detection runs."""
    debit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2099, 3, 1),
        amount=Decimal("500.00"), direction="out", raw_merchant="Amazon",
    )
    credit_id = _insert(
        write_conn, account_id=AMEX_CC, date=date(2099, 3, 15),
        amount=Decimal("500.00"), direction="in", raw_merchant="Amazon Mumbai",
        is_refund=True,  # user-overridden to TRUE before detection
        linked_txn_id=debit_id,
    )
    r = detect_for_account(AMEX_CC, since=date(2099, 2, 1), _conn_for_test=write_conn)
    flags = _select_flags(write_conn, credit_id)
    assert flags["is_refund"] is True   # untouched
    assert flags["linked_txn_id"] == debit_id   # untouched
    assert r.refunds_linked == 0   # detector didn't re-process this row
