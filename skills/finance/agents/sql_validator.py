"""Static SQL validator for the W4.1 SQL agent.

Defense-in-depth on top of the readonly role's GRANT enforcement (W3.5):
- single statement only
- top-level statement key must be `select` (sqlglot's classification)
- every referenced table must be in the caller-provided allowlist

This catches a generated SQL going off the rails (e.g. Groq hallucinating a
DELETE) before it touches the wire — both as a UX win (fail fast with a clear
reason) and as a redundancy. Role GRANTs are the durable boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None
    statement_type: str | None = None


def validate_sql(sql: str, allowed_tables: set[str]) -> ValidationResult:
    """Validate a single SELECT against the allowed-tables set.

    Returns ValidationResult(ok=True, statement_type='select') on success,
    or ValidationResult(ok=False, reason=<human-readable why>) on rejection.
    """
    if not sql or not sql.strip():
        return ValidationResult(ok=False, reason="empty SQL")

    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except ParseError as e:
        return ValidationResult(ok=False, reason=f"parse error: {e}")

    non_null = [s for s in statements if s is not None]
    if len(non_null) == 0:
        return ValidationResult(ok=False, reason="no parseable statement")
    if len(non_null) > 1:
        return ValidationResult(
            ok=False,
            reason="only a single statement is allowed (multi-statement input rejected)",
        )

    stmt = non_null[0]
    if stmt.key != "select":
        return ValidationResult(
            ok=False,
            reason=f"only SELECT is allowed; got {stmt.key.upper()}",
            statement_type=stmt.key,
        )

    for table in stmt.find_all(exp.Table):
        # tables in postgres can be schema-qualified; reject anything not in our flat allowlist
        name = table.name
        if name not in allowed_tables:
            qualified = (
                f"{table.db}.{name}" if table.db else name
            )
            return ValidationResult(
                ok=False,
                reason=f"table {qualified!r} is not on the allowlist {sorted(allowed_tables)}",
                statement_type="select",
            )

    return ValidationResult(ok=True, statement_type="select")
