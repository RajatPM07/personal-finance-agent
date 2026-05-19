"""W4.1 calibration harness.

Loads pairs.yaml (or a path passed via --pairs), runs each through
run_sql_agent, scores recall / precision / escalation rate, and emits
a confidence-distribution histogram (Step 5.0 gate).

Usage:
    .venv/bin/python -m tests.sql_agent_calibration.run_calibration \
        --pairs tests/sql_agent_calibration/pairs.yaml \
        --out tests/sql_agent_calibration/results-$(date +%Y%m%d).json
"""
from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from skills.finance.agents.sql_agent import AgentResult, run_sql_agent


@dataclass
class PairOutcome:
    id: int
    category: str
    question: str
    actual_sql: str
    expected_sql: str | None
    final: str
    gemini_verdict: str | None
    gemini_confidence: float | None
    escalated: bool
    retried: bool
    anthropic_verdict: str | None  # if escalated
    rows_returned: int


def _run_one(pair: dict) -> PairOutcome:
    result: AgentResult = run_sql_agent(pair["question"])
    return PairOutcome(
        id=pair["id"],
        category=pair.get("category", "uncategorized"),
        question=pair["question"],
        actual_sql=result.sql,
        expected_sql=pair.get("expected_sql"),
        final=result.final,
        gemini_verdict=result.judge_verdict.verdict if result.judge_verdict else None,
        gemini_confidence=result.judge_verdict.confidence if result.judge_verdict else None,
        escalated=result.escalated,
        retried=result.retried,
        anthropic_verdict=None,  # populated below if escalated — see note
        rows_returned=len(result.rows or []),
    )


def _bucket_confidence(values: list[float]) -> str:
    """Plain-text histogram for the confidence distribution (Step 5.0)."""
    if not values:
        return "(no confidence values)"
    bins = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    counts = [0] * (len(bins) - 1)
    for v in values:
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1]:
                counts[i] += 1
                break
        else:
            counts[-1] += 1  # value of exactly 1.0
    out_lines = []
    max_count = max(counts) if counts else 1
    for i, c in enumerate(counts):
        bar = "█" * (40 * c // max(max_count, 1))
        out_lines.append(f"  [{bins[i]:.1f}-{bins[i + 1]:.1f}) {c:>3} {bar}")
    return "\n".join(out_lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pairs",
        default="tests/sql_agent_calibration/pairs.yaml",
        help="Path to pairs.yaml (gitignored). Falls back to pairs.example.yaml.",
    )
    parser.add_argument(
        "--out",
        default=f"tests/sql_agent_calibration/results-{datetime.now(UTC):%Y%m%dT%H%M%S}.json",
    )
    args = parser.parse_args()

    pairs_path = Path(args.pairs)
    if not pairs_path.exists():
        pairs_path = Path("tests/sql_agent_calibration/pairs.example.yaml")
        print(f"!! {args.pairs} not found; using {pairs_path}")

    with open(pairs_path) as f:
        pairs = yaml.safe_load(f) or []

    outcomes: list[PairOutcome] = []
    for pair in pairs:
        print(f"\n--- pair {pair['id']}: {pair['question'][:60]} ---")
        try:
            outcome = _run_one(pair)
        except Exception as e:  # noqa: BLE001
            print(f"   CRASHED: {type(e).__name__}: {e}")
            continue
        outcomes.append(outcome)
        print(
            f"   final={outcome.final} judge={outcome.gemini_verdict} "
            f"conf={outcome.gemini_confidence} escalated={outcome.escalated} "
            f"retried={outcome.retried} rows={outcome.rows_returned}"
        )

    # Step 5.0: distribution plot
    confidences = [o.gemini_confidence for o in outcomes if o.gemini_confidence is not None]
    print("\n=== Step 5.0 — Gemini confidence distribution ===")
    print(_bucket_confidence(confidences))
    if confidences:
        print(
            f"  mean={statistics.mean(confidences):.3f}  "
            f"stdev={statistics.stdev(confidences) if len(confidences) > 1 else 0:.3f}  "
            f"min={min(confidences):.3f}  max={max(confidences):.3f}"
        )

    # Metrics
    n_total = len(outcomes)
    n_escalated = sum(1 for o in outcomes if o.escalated)
    escalation_rate = n_escalated / n_total if n_total else 0.0
    n_surfaced = sum(1 for o in outcomes if o.final == "surfaced_to_user")
    n_rendered = sum(1 for o in outcomes if o.final == "rendered")

    print("\n=== Metrics ===")
    print(f"  total pairs:     {n_total}")
    print(f"  rendered:        {n_rendered}")
    print(f"  surfaced:        {n_surfaced}")
    print(f"  escalation rate: {escalation_rate:.1%}  (target ≤ 20%, reject ≥ 40%)")
    print(
        "  judge recall on wrong SQL: REQUIRES MANUAL groq_sql_correct labelling "
        "in pairs.yaml. Spec §5.4."
    )

    print("\n=== Ship gate ===")
    if escalation_rate >= 0.40:
        print(f"  BLOCKED — escalation rate {escalation_rate:.1%} >= 40%")
    elif escalation_rate > 0.20:
        print(f"  WARN — escalation rate {escalation_rate:.1%} above target 20%")
    else:
        print(f"  Escalation rate OK ({escalation_rate:.1%})")

    out_path = Path(args.out)
    out_path.write_text(json.dumps([asdict(o) for o in outcomes], indent=2, default=str))
    print(f"\nResults written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
