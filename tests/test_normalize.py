"""Unit tests for the ICICI-CC merchant normalizer.

Anchored to the ICICI-CC signature (``<11-digit ref> MERCHANT <points> [fx]``).
Two guarantees under test: (a) it collapses the per-txn ref + reward-points so
identical merchants share one key, and (b) it is a strict no-op for every other
statement format — the safety property that lets us run it across the whole
transactions table without corrupting non-ICICI rows.
"""
from __future__ import annotations

import pytest

from skills.finance.categorization.normalize import normalize_merchant

# --- ICICI-CC strings: ref + points stripped, body canonicalized -----------

@pytest.mark.parametrize(
    "raw,expected",
    [
        # Same merchant, different ref + points -> one stable key.
        ("13483665323 AMAZON PAY IN E COMMERC BANGALORE 242",
         "AMAZON PAY IN E COMMERC BANGALORE"),
        ("13290727017 AMAZON PAY IN E COMMERC BANGALORE 158",
         "AMAZON PAY IN E COMMERC BANGALORE"),
        # Grocery vs retail Amazon deliberately stay distinct (Groceries vs Shopping).
        ("12961451639 AMAZON PAY IN GROCERY BANGALORE IN 104",
         "AMAZON PAY IN GROCERY BANGALORE IN"),
        ("13499126363 MYNTRA DESIGNS PRIVATE Bangalore IN 133",
         "MYNTRA DESIGNS PRIVATE Bangalore IN"),
        # Zero points (charges / interest lines).
        ("13032810469 Interest Charges 0", "Interest Charges"),
        ("13032810477 IGST-CI@18% 0", "IGST-CI@18%"),
        # Negative points (credits / reversals).
        ("13330804940 AMAZON PAY IN E COMMERC BANGALORE -42",
         "AMAZON PAY IN E COMMERC BANGALORE"),
        # Foreign-currency row: strip points AND the trailing fx amount.
        ("12903580231 KLINGAI.COM SINGAPORE SG* 94 25.99",
         "KLINGAI.COM SINGAPORE SG*"),
    ],
)
def test_icici_cc_strips_ref_and_points(raw, expected):
    assert normalize_merchant(raw) == expected


def test_identical_merchants_collapse_to_one_key():
    variants = [
        "13483665323 AMAZON PAY IN E COMMERC BANGALORE 242",
        "13290727017 AMAZON PAY IN E COMMERC BANGALORE 158",
        "12656857786 AMAZON PAY IN E COMMERC BANGALORE 105",
    ]
    keys = {normalize_merchant(v) for v in variants}
    assert keys == {"AMAZON PAY IN E COMMERC BANGALORE"}


# --- Non-ICICI-CC strings: strict no-op ------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        # ICICI Savings xls: 12-digit lead but no trailing integer.
        "236913008996 FD clos 17-03-2026 AAYUSHI SHUKLA",
        # Savings: digits followed by ':' not whitespace.
        "236901503315:Int.Pd:30-03-2026 to 29-06-2026",
        # AMEX: no leading digit run (double-spaces preserved verbatim).
        "ANTHROPIC               SAN FRANCISCO",
        # Paytm UPI: leading token only 2 digits (< 10).
        "03 10 Number Manohar Dairy Resturant",
        # PhonePe: masked account, no leading digits.
        "******0577",
        # Savings opt: pipe-delimited, no leading digit run.
        "Credit trxn | CMS/ SALARY APR 2026/ICICI LOMBARD GIC LTD | PERSON",
        # Plain salary memo (no ref/points shape).
        "INF/000175600906/SALARY FOR JAN-2026 90004536",
        # BBPS marker (in-direction CC credit) — no leading ref, unchanged.
        "BBPS Payment received",
    ],
)
def test_non_icici_cc_is_identity(raw):
    assert normalize_merchant(raw) == raw


def test_none_and_empty_pass_through():
    assert normalize_merchant(None) is None
    assert normalize_merchant("") == ""


def test_double_space_preserved_for_non_matching():
    # AMEX descriptors use column padding; a no-op must not collapse it.
    s = "ANTHROPIC               SAN FRANCISCO"
    assert normalize_merchant(s) == s
