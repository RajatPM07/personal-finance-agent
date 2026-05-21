"""W5.1 refund + self-transfer detector.

Per docs/superpowers/specs/2026-05-21-refund-detection-design.md.

Pure heuristic detection (no LLM). Populates is_refund, is_self_transfer,
linked_txn_id on transactions rows after ingestion. Invoked from
pipeline.py via adb() so the synchronous DB calls don't block the async loop.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

import yaml

_PATTERNS_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "self_transfer_patterns.yaml"
)


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
