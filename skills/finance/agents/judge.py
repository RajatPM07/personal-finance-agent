"""Judge prompt builder + response parser.

Pure functions only — the LLM call itself lives in sql_agent.py. Splitting
the prompt construction and parsing out keeps them unit-testable without
network, and lets the calibration harness reuse the exact same prompt that
ships in production.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

Verdict = Literal["ok", "wrong", "uncertain"]


@dataclass(frozen=True)
class JudgeVerdict:
    verdict: Verdict
    confidence: float
    reason: str


_PROMPT_TEMPLATE = """\
You are a SQL reviewer. Given a natural language question, the SQL that was
generated to answer it, and the first 3 rows of the result, decide whether
the SQL faithfully answers the question. Be strict.

- If the SQL touches the wrong tables, applies the wrong filter, returns the
  wrong aggregation shape, or omits a required clause, mark it WRONG.
- If you cannot tell from the question and the result rows whether the SQL is
  right, mark it UNCERTAIN.
- Otherwise mark it OK.

Schema (relevant tables):
{schema_excerpt}

Question: "{question}"

Generated SQL:
```sql
{sql}
```

First 3 result rows (JSON):
{result_preview_json}

Respond with a single JSON object and nothing else:
{{"verdict": "ok" | "wrong" | "uncertain", "confidence": 0.0-1.0, "reason": "<one sentence>"}}
"""


def build_judge_prompt(
    question: str,
    sql: str,
    result_preview: list[dict[str, Any]],
    schema_excerpt: str,
) -> str:
    """Render the judge prompt. Result preview capped at first 3 rows per
    spec §4.1 ('First N=3'). Excess rows are silently dropped — the cap is
    intentional; the judge doesn't need full data to assess correctness."""
    capped = result_preview[:3]
    return _PROMPT_TEMPLATE.format(
        question=question,
        sql=sql,
        result_preview_json=json.dumps(capped, default=str, indent=2),
        schema_excerpt=schema_excerpt,
    )


_CODEFENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_judge_response(raw: str) -> JudgeVerdict:
    """Parse the judge's JSON response. Tolerates markdown codefence wrappers
    (some providers add ```json ... ``` even when asked for raw JSON)."""
    stripped = raw.strip()
    m = _CODEFENCE_RE.match(stripped)
    if m:
        stripped = m.group(1).strip()

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(f"judge response is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"judge response must be a JSON object, got {type(data).__name__}")

    for required in ("verdict", "confidence", "reason"):
        if required not in data:
            raise ValueError(f"judge response missing required field: {required!r}")

    verdict = data["verdict"]
    if verdict not in ("ok", "wrong", "uncertain"):
        raise ValueError(f"verdict must be one of ok/wrong/uncertain; got {verdict!r}")

    try:
        confidence = float(data["confidence"])
    except (TypeError, ValueError) as e:
        raise ValueError(f"confidence must be a number; got {data['confidence']!r}") from e
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0.0, 1.0]; got {confidence}")

    reason = str(data["reason"])

    return JudgeVerdict(verdict=verdict, confidence=confidence, reason=reason)
