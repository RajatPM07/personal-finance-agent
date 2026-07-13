from skills.finance.lib import users


def test_rajat_and_ayushi_ids_distinct():
    assert users.RAJAT_USER_ID != users.AYUSHI_USER_ID


def test_maps_rajat_chat_to_rajat(monkeypatch):
    monkeypatch.setattr(users.settings, "telegram_chat_id_rajat", "111")
    monkeypatch.setattr(users.settings, "telegram_chat_id_ayushi", "222")
    assert users.user_id_for_chat("111") == users.RAJAT_USER_ID
    assert users.user_id_for_chat(111) == users.RAJAT_USER_ID   # int accepted


def test_maps_ayushi_chat_to_ayushi(monkeypatch):
    monkeypatch.setattr(users.settings, "telegram_chat_id_rajat", "111")
    monkeypatch.setattr(users.settings, "telegram_chat_id_ayushi", "222")
    assert users.user_id_for_chat("222") == users.AYUSHI_USER_ID


def test_unknown_chat_returns_none(monkeypatch):
    monkeypatch.setattr(users.settings, "telegram_chat_id_rajat", "111")
    monkeypatch.setattr(users.settings, "telegram_chat_id_ayushi", "222")
    assert users.user_id_for_chat("999") is None


def test_empty_ayushi_id_never_matches(monkeypatch):
    # Guard: an unset TELEGRAM_CHAT_ID_AYUSHI ("") must not authorize chat "".
    monkeypatch.setattr(users.settings, "telegram_chat_id_rajat", "111")
    monkeypatch.setattr(users.settings, "telegram_chat_id_ayushi", "")
    assert users.user_id_for_chat("") is None
