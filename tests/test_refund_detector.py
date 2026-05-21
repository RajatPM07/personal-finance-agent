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
