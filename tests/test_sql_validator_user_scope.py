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


def test_rejects_implicit_cross_join():
    # transactions is left unfiltered and implicitly cartesian-joined with
    # accounts, which IS filtered by the caller's user_id -- this must not
    # be enough to leak other users' transactions.
    sql = (
        "SELECT sum(t.amount) FROM transactions t, accounts a "
        f"WHERE a.user_id = '{UID}'"
    )
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert not r.ok
    assert "cross-join" in r.reason.lower()


def test_accepts_explicit_join_with_user_scope():
    sql = (
        "SELECT sum(t.amount) FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id "
        f"WHERE a.user_id = '{UID}'"
    )
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert r.ok


def test_rejects_constant_only_join_on():
    # `JOIN accounts a ON a.user_id = '<caller>'` puts the user filter in the
    # ON clause with no column=column link, so `transactions t` is left
    # unfiltered and every user's rows leak -- a disguised cross-join that
    # must be rejected even though the caller UUID is present.
    sql = (
        "SELECT t.amount FROM transactions t "
        f"JOIN accounts a ON a.user_id = '{UID}'"
    )
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert not r.ok
    assert "cross-join" in r.reason.lower() or "column equality" in r.reason.lower()


def test_accepts_join_with_link_and_extra_predicate():
    # A real column link plus an extra user_id predicate in the ON is fine.
    sql = (
        "SELECT t.amount FROM transactions t "
        f"JOIN accounts a ON a.id = t.account_id AND a.user_id = '{UID}' "
        f"WHERE t.user_id = '{UID}'"
    )
    r = validate_sql(sql, TABLES, require_user_id=UID)
    assert r.ok
