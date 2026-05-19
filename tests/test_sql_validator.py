"""W4.1 SQL static validator — rejects anything that isn't a single SELECT
against the allowlisted tables. Defense-in-depth on top of the role GRANTs
enforced by the readonly Postgres user (W3.5)."""
from __future__ import annotations

import pytest

from skills.finance.agents.sql_validator import ValidationResult, validate_sql

ALLOWED = {
    "transactions", "accounts", "categories", "assets", "liabilities",
    "users", "ingestion_log", "commitments", "income_events",
}


def test_simple_select_allowed():
    r = validate_sql("SELECT count(*) FROM transactions", ALLOWED)
    assert r.ok is True
    assert r.statement_type == "select"


def test_select_with_join_allowed():
    r = validate_sql(
        "SELECT t.amount, c.name FROM transactions t "
        "JOIN categories c ON t.category_id = c.id",
        ALLOWED,
    )
    assert r.ok is True


def test_insert_rejected():
    r = validate_sql(
        "INSERT INTO transactions (user_id, date, amount, direction) "
        "VALUES (gen_random_uuid(), CURRENT_DATE, 1, 'out')",
        ALLOWED,
    )
    assert r.ok is False
    assert "select" in (r.reason or "").lower() or "insert" in (r.reason or "").lower()


@pytest.mark.parametrize("sql", [
    "UPDATE transactions SET amount = 0",
    "DELETE FROM transactions WHERE id IS NOT NULL",
    "DROP TABLE transactions",
    "ALTER TABLE transactions ADD COLUMN x int",
    "TRUNCATE transactions",
    "CREATE TABLE x (id int)",
    "GRANT SELECT ON transactions TO public",
])
def test_non_select_rejected(sql):
    r = validate_sql(sql, ALLOWED)
    assert r.ok is False


def test_multi_statement_rejected():
    r = validate_sql("SELECT 1; DELETE FROM users", ALLOWED)
    assert r.ok is False
    assert "single" in (r.reason or "").lower() or "multi" in (r.reason or "").lower()


def test_off_allowlist_table_rejected():
    r = validate_sql("SELECT * FROM pg_catalog.pg_user", ALLOWED)
    assert r.ok is False
    assert "allow" in (r.reason or "").lower() or "pg_user" in (r.reason or "").lower()


def test_unrecognised_table_rejected():
    r = validate_sql("SELECT * FROM secret_table", ALLOWED)
    assert r.ok is False


def test_malformed_sql_rejected():
    r = validate_sql("SELEKT * FROM transactions", ALLOWED)
    assert r.ok is False
    assert "parse" in (r.reason or "").lower() or "syntax" in (r.reason or "").lower()


def test_empty_string_rejected():
    r = validate_sql("", ALLOWED)
    assert r.ok is False


def test_whitespace_only_rejected():
    r = validate_sql("   \n  ", ALLOWED)
    assert r.ok is False


def test_validation_result_is_dataclass():
    r = validate_sql("SELECT 1 FROM transactions", ALLOWED)
    assert isinstance(r, ValidationResult)
    assert isinstance(r.ok, bool)
    assert r.reason is None or isinstance(r.reason, str)
