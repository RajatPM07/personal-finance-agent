"""Regression guard for `config/db_schema_for_judge.md`.

W4.1 calibration triage revealed that wording like "well-written SQL still
includes the filter" encouraged Groq to hallucinate a placeholder UUID into
`WHERE user_id = 'your_user_id'`, which then failed at Postgres execute time
with `invalid input syntax for type uuid`.

This test is brittle by design: any future edit to the user_id guidance must
deliberately update this guard, forcing a conscious review of whether the
single-user no-filter contract is preserved.
"""
from __future__ import annotations

from pathlib import Path

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "db_schema_for_judge.md"
)


def _read_schema() -> str:
    with open(_SCHEMA_PATH) as f:
        return f.read()


def test_schema_excerpt_does_not_encourage_user_id_filter() -> None:
    """Old phrasing must not creep back in via copy-paste or revert."""
    text = _read_schema()
    assert "includes the filter" not in text, (
        "Old user_id-encouraging phrasing detected. The schema excerpt must "
        "NOT tell the SQL generator to include a user_id filter — see "
        "calibration triage notes."
    )
    assert "well-written SQL still includes" not in text, (
        "Old user_id-encouraging phrasing detected."
    )


def test_schema_excerpt_explicitly_forbids_user_id_filter() -> None:
    """Affirmative guard: the DO NOT instruction must be present."""
    text = _read_schema()
    assert "DO NOT include" in text, (
        "Schema excerpt must contain an explicit 'DO NOT include' instruction "
        "about user_id filtering."
    )
    # The instruction must specifically reference user_id (not just be a
    # generic DO NOT somewhere in the file).
    do_not_idx = text.index("DO NOT include")
    window = text[do_not_idx : do_not_idx + 200]
    assert "user_id" in window, (
        "The 'DO NOT include' instruction must reference user_id specifically."
    )
