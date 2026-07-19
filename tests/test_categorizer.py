"""Unit tests for the LLM merchant categorizer. The llm() call is mocked — no
network, no live model. Verifies batching/dedup, validation against the allowed
category set, and graceful fallback on every failure mode."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from skills.finance.categorization import categorizer
from skills.finance.categorization.categorizer import categorize_merchants

ALLOWED = ["Food Delivery", "Shopping", "Groceries", "Needs Review", "Self Transfer"]


def _resp(payload) -> MagicMock:
    """Build a fake litellm response whose content is `payload` (dict → JSON,
    str → verbatim so we can inject malformed/ fenced bodies)."""
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


def test_maps_merchants_to_allowed_categories():
    with patch.object(categorizer, "llm") as m:
        m.return_value = _resp({"Swiggy": "Food Delivery", "Myntra": "Shopping"})
        out = categorize_merchants(["Swiggy", "Myntra"], ALLOWED)
    assert out == {"Swiggy": "Food Delivery", "Myntra": "Shopping"}


def test_dedups_and_returns_every_distinct_merchant():
    with patch.object(categorizer, "llm") as m:
        m.return_value = _resp({"Swiggy": "Food Delivery"})
        out = categorize_merchants(["Swiggy", "Swiggy", "Swiggy"], ALLOWED)
    # Deduped to one LLM call; result keyed by the single distinct merchant.
    assert out == {"Swiggy": "Food Delivery"}
    assert m.call_count == 1


def test_batches_by_batch_size():
    # 5 merchants, batch_size 2 → 3 calls. Each call echoes its batch verbatim.
    def _echo(_task, prompt, **_kw):
        names = [ln[2:] for ln in prompt.splitlines() if ln.startswith("- ")]
        return _resp({n: "Shopping" for n in names})

    with patch.object(categorizer, "llm", side_effect=_echo) as m:
        out = categorize_merchants([f"m{i}" for i in range(5)], ALLOWED, batch_size=2)
    assert m.call_count == 3
    assert len(out) == 5
    assert set(out.values()) == {"Shopping"}


def test_unknown_category_falls_back_to_needs_review():
    with patch.object(categorizer, "llm") as m:
        m.return_value = _resp({"MysteryCorp": "Crypto"})  # not in ALLOWED
        out = categorize_merchants(["MysteryCorp"], ALLOWED)
    assert out == {"MysteryCorp": "Needs Review"}


def test_missing_merchant_in_response_falls_back():
    with patch.object(categorizer, "llm") as m:
        m.return_value = _resp({"Swiggy": "Food Delivery"})  # Zepto omitted
        out = categorize_merchants(["Swiggy", "Zepto"], ALLOWED)
    assert out["Swiggy"] == "Food Delivery"
    assert out["Zepto"] == "Needs Review"


def test_malformed_json_falls_back_whole_batch():
    with patch.object(categorizer, "llm") as m:
        m.return_value = _resp("this is not json{{{")
        out = categorize_merchants(["A", "B"], ALLOWED)
    assert out == {"A": "Needs Review", "B": "Needs Review"}


def test_llm_raises_falls_back_whole_batch():
    with patch.object(categorizer, "llm", side_effect=RuntimeError("boom")):
        out = categorize_merchants(["A", "B"], ALLOWED)
    assert out == {"A": "Needs Review", "B": "Needs Review"}


def test_codefence_wrapped_json_is_parsed():
    with patch.object(categorizer, "llm") as m:
        m.return_value = _resp('```json\n{"Swiggy": "Food Delivery"}\n```')
        out = categorize_merchants(["Swiggy"], ALLOWED)
    assert out == {"Swiggy": "Food Delivery"}


def test_fallback_must_be_allowed():
    with pytest.raises(ValueError):
        categorize_merchants(["x"], ALLOWED, fallback="Nonexistent")


def test_empty_and_none_merchants_ignored():
    with patch.object(categorizer, "llm") as m:
        m.return_value = _resp({"Swiggy": "Food Delivery"})
        out = categorize_merchants(["Swiggy", "", None], ALLOWED)  # type: ignore[list-item]
    assert out == {"Swiggy": "Food Delivery"}


def test_no_merchants_makes_no_llm_call():
    with patch.object(categorizer, "llm") as m:
        out = categorize_merchants([], ALLOWED)
    assert out == {}
    m.assert_not_called()


def test_pause_sleeps_between_batches_only():
    # 3 batches → sleeps twice (not before the first, not after the last).
    with patch.object(categorizer, "llm") as m, \
         patch.object(categorizer.time, "sleep") as sleep:
        m.return_value = _resp({})  # everything falls back; irrelevant here
        categorize_merchants([f"m{i}" for i in range(3)], ALLOWED, batch_size=1, pause_s=2.0)
    assert sleep.call_count == 2
    sleep.assert_called_with(2.0)


def test_default_pause_does_not_sleep():
    with patch.object(categorizer, "llm") as m, \
         patch.object(categorizer.time, "sleep") as sleep:
        m.return_value = _resp({"A": "Shopping"})
        categorize_merchants(["A", "B"], ALLOWED, batch_size=1)
    sleep.assert_not_called()


class TestOverrides:
    """Deterministic overrides in the backfill script (rent / PPF / FD strings
    the LLM mis-guesses)."""

    def test_rent_account_number_maps_to_rent(self):
        from scripts.backfill_categorization import override_category
        assert override_category("Bank Account XXXXXXXXXXX4891") == "Rent"

    def test_ppf_and_fd_map_to_self_transfer(self):
        from scripts.backfill_categorization import override_category
        assert override_category("Trf to PPF 000418393630") == "Self Transfer"
        assert override_category("TRF TO FD no. 236913011506") == "Self Transfer"

    def test_case_insensitive(self):
        from scripts.backfill_categorization import override_category
        assert override_category("trf to fd XYZ") == "Self Transfer"

    def test_unknown_merchant_has_no_override(self):
        from scripts.backfill_categorization import override_category
        assert override_category("Swiggy") is None

    def test_cc_line_items_map_to_bank_charges(self):
        # ICICI CC interest / tax lines the LLM is told not to guess.
        from scripts.backfill_categorization import override_category
        assert override_category("Interest Charges") == "Bank Charges"
        assert override_category("Interest Amount Amortization -") == "Bank Charges"
        assert override_category("IGST-CI@18%") == "Bank Charges"

    def test_override_targets_are_valid_categories(self):
        # Every override value must be a real taxonomy category, else apply fails.
        from scripts.backfill_categorization import KNOWN_OVERRIDES
        for _needle, cat in KNOWN_OVERRIDES:
            assert cat in {"Rent", "Self Transfer", "Bank Charges"}


class TestFinalizeMapping:
    """finalize_mapping merges overrides + a base map (LLM or cached JSON),
    guaranteeing every merchant lands on a valid category."""

    ALLOWED = {"Shopping", "Rent", "Self Transfer", "Needs Review"}

    def test_override_wins_over_base_map(self):
        from scripts.backfill_categorization import finalize_mapping
        out = finalize_mapping(
            ["Bank Account XXXXXXXXXXX4891"],
            {"Bank Account XXXXXXXXXXX4891": "Shopping"},  # LLM/cached said Shopping
            self.ALLOWED,
        )
        assert out["Bank Account XXXXXXXXXXX4891"] == "Rent"  # override wins

    def test_valid_base_map_value_used(self):
        from scripts.backfill_categorization import finalize_mapping
        out = finalize_mapping(["Myntra"], {"Myntra": "Shopping"}, self.ALLOWED)
        assert out["Myntra"] == "Shopping"

    def test_unknown_or_missing_falls_back(self):
        from scripts.backfill_categorization import finalize_mapping
        out = finalize_mapping(
            ["Ghost", "Weird"],
            {"Ghost": "Crypto"},  # invalid category; "Weird" absent (stale cache)
            self.ALLOWED,
        )
        assert out == {"Ghost": "Needs Review", "Weird": "Needs Review"}
