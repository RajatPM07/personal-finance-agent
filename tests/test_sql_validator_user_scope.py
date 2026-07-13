from skills.finance.agents.sql_validator import validate_sql

TABLES = {"transactions", "accounts", "categories"}
UID = "00000000-0000-0000-0000-000000000002"


def test_rejects_query_missing_user_id():
    sql = "SELECT sum(amount) FROM transactions WHERE direction='out'"
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert not r.ok and "user_id" in r.reason.lower()


def test_accepts_query_with_correct_user_id():
    sql = f"SELECT sum(amount) FROM transactions WHERE user_id = '{UID}' AND direction='out'"
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert r.ok


def test_rejects_query_with_foreign_user_id():
    other = "00000000-0000-0000-0000-000000000001"
    sql = f"SELECT sum(amount) FROM transactions WHERE user_id = '{other}'"
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert not r.ok


def test_require_user_id_none_preserves_legacy_behavior():
    sql = "SELECT sum(amount) FROM transactions"
    assert validate_sql(sql, TABLES, require_user_id=None).ok
