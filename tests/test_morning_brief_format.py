"""Unit tests for the pure formatting/computation layer of the morning brief."""
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from skills.finance.nudging.morning_brief import (
    BriefData,
    NewTxn,
    _inr,
    compute_pacing_pct,
    format_brief,
    top_mover,
)

IST = ZoneInfo("Asia/Kolkata")
JUL_4 = datetime(2026, 7, 4, 9, 0, tzinfo=IST)


def _base_data(**overrides: Any) -> BriefData:
    defaults: dict[str, Any] = dict(
        now_ist=JUL_4,
        new_txns=[],
        mtd_total=Decimal("38400"),
        monthly_avg=Decimal("260000"),
        months_of_history=6,
        top_category="Dining Out",
        top_category_mtd=Decimal("9200"),
        top_category_avg=Decimal("22575"),
    )
    defaults.update(overrides)
    return BriefData(**defaults)


class TestInr:
    def test_whole_rupees_grouped(self) -> None:
        assert _inr(Decimal("38400")) == "₹38,400"

    def test_rounds_paise(self) -> None:
        assert _inr(Decimal("640.75")) == "₹641"

    def test_zero(self) -> None:
        assert _inr(Decimal("0")) == "₹0"


class TestComputePacingPct:
    def test_over_pace(self) -> None:
        # expected at day 4/31 of a 260k avg month = 33,548; mtd 38,400 → +14%
        pct = compute_pacing_pct(Decimal("38400"), Decimal("260000"), 4, 31)
        assert pct is not None
        assert round(pct) == 14

    def test_under_pace_negative(self) -> None:
        pct = compute_pacing_pct(Decimal("20000"), Decimal("260000"), 4, 31)
        assert pct is not None
        assert pct < 0

    def test_no_baseline_returns_none(self) -> None:
        assert compute_pacing_pct(Decimal("38400"), Decimal("0"), 4, 31) is None


class TestTopMover:
    def test_picks_largest_positive_delta_vs_prorated_avg(self) -> None:
        mtd: dict[str, Decimal] = {"Dining Out": Decimal("9200"), "Groceries": Decimal("1000")}
        avg: dict[str, Decimal] = {"Dining Out": Decimal("22575"), "Groceries": Decimal("7942")}
        # day 4/31 prorated: Dining 2913 → delta +6287; Groceries 1025 → delta -25
        result = top_mover(mtd, avg, 4, 31)
        assert result is not None
        name, cat_mtd, cat_avg = result
        assert name == "Dining Out"
        assert cat_mtd == Decimal("9200")
        assert cat_avg == Decimal("22575")

    def test_no_positive_delta_returns_none(self) -> None:
        mtd = {"Groceries": Decimal("100")}
        avg: dict[str, Decimal] = {"Groceries": Decimal("7942")}
        assert top_mover(mtd, avg, 4, 31) is None

    def test_category_without_baseline_ignored(self) -> None:
        mtd = {"Brand New Cat": Decimal("99999")}
        avg: dict[str, Decimal] = {}
        assert top_mover(mtd, avg, 4, 31) is None

    def test_empty_inputs(self) -> None:
        assert top_mover({}, {}, 4, 31) is None


class TestFormatBriefNewTxns:
    def test_new_txns_variant(self) -> None:
        data = _base_data(new_txns=[
            NewTxn(merchant="Amazon", amount=Decimal("3180"), category="Shopping"),
            NewTxn(merchant="Uber", amount=Decimal("1000"), category="Transport"),
            NewTxn(merchant="Swiggy", amount=Decimal("640"), category="Food Delivery"),
        ])
        text = format_brief(data)
        assert text.startswith("₹ Brief · Jul 4")
        assert "3 new txns: ₹4,820 out" in text
        assert "• Amazon ₹3,180 · Shopping" in text
        assert "• Swiggy ₹640 · Food Delivery" in text
        assert "July so far: ₹38,400" in text
        # pacing line present with a signed percentage
        assert "vs avg pace" in text

    def test_caps_at_five_txns_with_more_line(self) -> None:
        txns = [
            NewTxn(merchant=f"M{i}", amount=Decimal(1000 - i), category="Shopping")
            for i in range(7)
        ]
        text = format_brief(_base_data(new_txns=txns))
        assert "7 new txns" in text
        assert text.count("•") == 5
        assert "+2 more" in text

    def test_null_category_renders_needs_review(self) -> None:
        data = _base_data(new_txns=[
            NewTxn(merchant="Mystery Shop", amount=Decimal("500"), category=None),
        ])
        assert "• Mystery Shop ₹500 · Needs Review" in format_brief(data)

    def test_new_txns_with_zero_mtd_omits_pacing_suffix(self) -> None:
        data = _base_data(
            mtd_total=Decimal("0"),
            new_txns=[NewTxn(merchant="Swiggy", amount=Decimal("640"), category="Food Delivery")],
        )
        text = format_brief(data)
        assert "vs avg pace" not in text
        assert "-100%" not in text


class TestFormatBriefPacing:
    def test_pacing_variant_when_no_new_txns(self) -> None:
        text = format_brief(_base_data())
        assert text.startswith("₹ Brief · Jul 4")
        assert "July so far: ₹38,400" in text
        assert "vs 6-mo avg" in text
        assert "Top mover: Dining Out ₹9,200 (avg ₹22,575)" in text
        assert "new txns" not in text

    def test_no_baseline_omits_pacing_line(self) -> None:
        text = format_brief(_base_data(monthly_avg=Decimal("0"), months_of_history=0,
                                       top_category=None,
                                       top_category_mtd=Decimal("0"),
                                       top_category_avg=Decimal("0")))
        assert "July so far: ₹38,400" in text
        assert "no baseline yet" in text

    def test_no_top_mover_omits_line(self) -> None:
        text = format_brief(_base_data(top_category=None,
                                       top_category_mtd=Decimal("0"),
                                       top_category_avg=Decimal("0")))
        assert "Top mover" not in text

    def test_zero_mtd_shows_nothing_recorded(self) -> None:
        text = format_brief(_base_data(mtd_total=Decimal("0")))
        assert "₹0 — nothing recorded yet" in text
        assert "-100%" not in text
        assert "Top mover" not in text
