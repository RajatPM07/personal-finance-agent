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

import re as _re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

_UUID_RE = _re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None
    statement_type: str | None = None


def validate_sql(
    sql: str,
    allowed_tables: set[str],
    require_user_id: str | None = None,
) -> ValidationResult:
    """Validate a single SELECT against the allowed-tables set.

    Returns ValidationResult(ok=True, statement_type='select') on success,
    or ValidationResult(ok=False, reason=<human-readable why>) on rejection.

    When ``require_user_id`` is set (multi-user mode), the query MUST filter by
    ``user_id = '<that UUID>'``. This is an application-level tenancy guard
    (defense-in-depth alongside the LLM prompt) — a leak here would expose one
    user's finances to another. When ``require_user_id`` is None the behavior is
    identical to the legacy single-user validator.
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

    if require_user_id is not None:
        # Reject implicit comma cross-joins (and equivalent unlinked explicit
        # joins, e.g. bare CROSS JOIN with no ON/USING). sqlglot represents
        # `FROM a, b` as a `From(this=a)` plus a `joins=[Join(this=b)]` list
        # where the Join node has no `on`/`using` arg -- indistinguishable at
        # this level from `a CROSS JOIN b` with no condition. An unlinked
        # join lets one filtered table (e.g. accounts scoped by user_id)
        # cartesian-join in an *unfiltered* table (e.g. transactions),
        # leaking every user's rows through the join. An explicit
        # `JOIN ... ON`/`USING` is fine because it structurally ties the
        # unfiltered table's rows to the filtered one via the join key.
        for join in stmt.find_all(exp.Join):
            if join.args.get("on") is None and not join.args.get("using"):
                return ValidationResult(
                    ok=False,
                    reason=(
                        "implicit cross-join (comma-separated tables) is not "
                        "allowed under user scoping; use explicit JOIN ... ON"
                    ),
                    statement_type="select",
                )

        # Collect all string literals that appear in a `user_id = <literal>`
        # comparison. The caller's UUID must be present, and no *other*
        # UUID-shaped literal may be compared against user_id.
        user_id_values: list[str] = []
        for eq in stmt.find_all(exp.EQ):
            cols = [c.name for c in eq.find_all(exp.Column)]
            if "user_id" in cols:
                for lit in eq.find_all(exp.Literal):
                    if lit.is_string:
                        user_id_values.append(lit.this)
        if require_user_id not in user_id_values:
            return ValidationResult(
                ok=False,
                reason="query must filter every table by user_id = the caller's UUID",
                statement_type="select",
            )
        foreign = [
            v for v in user_id_values if _UUID_RE.match(v) and v != require_user_id
        ]
        if foreign:
            return ValidationResult(
                ok=False,
                reason=f"query references a foreign user_id: {foreign[0]}",
                statement_type="select",
            )

    return ValidationResult(ok=True, statement_type="select")
