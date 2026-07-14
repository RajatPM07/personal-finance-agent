"""Regression guard for `config/db_schema_for_judge.md`.

Task 5 (Ayushi onboarding) moved the agent to APPLICATION-LEVEL multi-user
separation: every query MUST be scoped to the caller's `user_id`, enforced both
by this schema instruction (so the LLM writes a compliant query) and by the
static validator's `require_user_id` guard (defense-in-depth). This inverts the
old single-user "DO NOT include a user_id filter" contract.

This test is brittle by design: any future edit to the user_id guidance must
deliberately update this guard, forcing a conscious review of whether the
multi-user MUST-filter contract is preserved. Weakening it back toward a
no-filter contract would silently expose one user's finances to the other.
"""
from __future__ import annotations

from pathlib import Path

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "db_schema_for_judge.md"
)


def _read_schema() -> str:
    with open(_SCHEMA_PATH) as f:
        return f.read()


def test_schema_excerpt_does_not_carry_old_single_user_contract() -> None:
    """The old single-user 'DO NOT include a user_id filter' phrasing must not
    creep back in via copy-paste or revert — it would defeat per-user scoping."""
    text = _read_schema()
    assert "DO NOT include a `user_id` filter" not in text, (
        "Old single-user no-filter phrasing detected. Under multi-user scoping "
        "every query MUST filter by user_id — see Task 5."
    )
    assert "well-written SQL still includes" not in text, (
        "Old user_id phrasing detected."
    )


def test_schema_excerpt_requires_user_id_filter() -> None:
    """Affirmative guard: the MUST-filter-by-user_id instruction must be present
    and must specifically reference user_id."""
    text = _read_schema()
    assert "MUST filter by user_id" in text, (
        "Schema excerpt must contain an explicit 'MUST filter by user_id' "
        "instruction for the multi-user contract."
    )
    must_idx = text.index("MUST filter by user_id")
    window = text[must_idx : must_idx + 300]
    assert "user_id" in window, (
        "The MUST-filter instruction must reference user_id specifically."
    )
