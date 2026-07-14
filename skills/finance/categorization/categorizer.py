"""LLM-backed merchant → category assignment (post-V1 sibling of the detector).

Groups distinct ``raw_merchant`` strings and asks the LLM (routing task
``merchant_categorization``) to map each to one of the user's existing category
names. Merchants are batched many-per-call to minimise API calls and token
cost — 385 distinct merchants become ~20 calls, not 385.

Pure logic — NO database access here. The backfill script
(``scripts/backfill_categorization.py``) owns fetching merchants, resolving
category names to ids, and writing. That keeps this module unit-testable with a
mocked ``llm()`` and no Supabase.

Graceful degradation (per design): any LLM error, malformed JSON, or a category
name outside the allowed set maps that merchant to the fallback category
(``Needs Review``) with a logged warning — the pass never crashes on one bad
batch or one bad merchant.
"""
from __future__ import annotations

import json
import logging
import re
import time

from skills.finance.lib.llm import llm

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 20
FALLBACK_CATEGORY = "Needs Review"

# Some providers wrap JSON in a ```json ... ``` fence even when asked for raw
# JSON. Mirror the judge's tolerance (skills/finance/agents/judge.py).
_CODEFENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

_SYSTEM = (
    "You are a transaction categorizer for an Indian personal-finance app. "
    "Given merchant / counterparty strings taken from bank and UPI statements, "
    "assign each to exactly ONE category from the provided list. Use Indian "
    "context: e.g. Swiggy/Zomato = Food Delivery, Blinkit/Instamart/Reliance "
    "Smart = Groceries, Myntra/Ajio = Shopping, Uber/Ola/BluSmart = Car & "
    "Transport, Airtel/Jio = Utilities. If you cannot confidently identify a "
    "real merchant — person names, unclear UPI handles, bare account numbers, "
    "numeric-only strings, or internal bank transfers — use 'Needs Review'. Do "
    "NOT guess 'Bank Charges' or 'EMI' for strings you cannot identify. Respond with ONLY a "
    "JSON object mapping each input merchant verbatim to a category name — no "
    "prose, no markdown."
)


def _build_prompt(merchants: list[str], allowed_categories: list[str]) -> str:
    return (
        "Allowed categories (choose exactly one per merchant):\n"
        + ", ".join(allowed_categories)
        + "\n\nMerchants to categorize:\n"
        + "\n".join(f"- {m}" for m in merchants)
        + '\n\nReturn a JSON object of the form '
        '{"<merchant>": "<category>", ...} that includes EVERY merchant above '
        "as a key, copied verbatim."
    )


def _parse_mapping(raw: str) -> dict[str, str]:
    """Parse the LLM's JSON object, tolerating a markdown codefence wrapper.
    Raises ValueError/JSONDecodeError on anything not a JSON object — the caller
    catches and falls back."""
    stripped = raw.strip()
    m = _CODEFENCE_RE.match(stripped)
    if m:
        stripped = m.group(1).strip()
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


def categorize_merchants(
    merchants: list[str],
    allowed_categories: list[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    fallback: str = FALLBACK_CATEGORY,
    pause_s: float = 0.0,
) -> dict[str, str]:
    """Map each distinct merchant string to an allowed category name.

    Dedups ``merchants`` (order-preserving), batches ``batch_size`` per LLM
    call, and validates every returned category is in ``allowed_categories``.
    The returned dict is guaranteed to contain EVERY distinct input merchant as
    a key — unmapped/invalid ones resolve to ``fallback``.

    ``pause_s`` sleeps between batches to respect free-tier per-minute token
    limits (Groq is 12k TPM; unpaced bursts trip 429s that would otherwise dump
    whole batches to the fallback). Default 0.0 keeps unit tests instant.

    ``fallback`` must itself be an allowed category, else the guarantee that all
    values are valid categories would be violated — we fail loud on that
    programming error rather than write an unknown category downstream.
    """
    allowed_set = set(allowed_categories)
    if fallback not in allowed_set:
        raise ValueError(
            f"fallback {fallback!r} must be one of allowed_categories"
        )

    # Order-preserving dedup; drop None/empty merchant strings.
    distinct: list[str] = []
    seen: set[str] = set()
    for m in merchants:
        if m and m not in seen:
            seen.add(m)
            distinct.append(m)

    result: dict[str, str] = {}
    for start in range(0, len(distinct), batch_size):
        if start > 0 and pause_s > 0:
            time.sleep(pause_s)
        batch = distinct[start : start + batch_size]
        try:
            resp = llm(
                "merchant_categorization",
                _build_prompt(batch, allowed_categories),
                system=_SYSTEM,
                response_format={"type": "json_object"},
            )
            mapping = _parse_mapping(resp.choices[0].message.content)
        except Exception as e:  # noqa: BLE001 — graceful degradation by design
            logger.warning(
                "categorization batch [%d:%d] failed (%s); mapping %d merchant(s) to %r",
                start, start + len(batch), type(e).__name__, len(batch), fallback,
            )
            for m in batch:
                result[m] = fallback
            continue

        for m in batch:
            cat = mapping.get(m)
            if cat not in allowed_set:
                logger.warning(
                    "merchant %r -> %s; using fallback %r",
                    m,
                    f"unknown category {cat!r}" if cat is not None else "missing from response",
                    fallback,
                )
                cat = fallback
            result[m] = cat

    return result
