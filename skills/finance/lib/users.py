"""User identity + Telegram-chat → DB user_id mapping.

Application-level user separation (no Postgres RLS). Single source of truth
for who is who. The chat→user map is derived from settings, so rotating a
chat id is a config change, not a code change.
"""
from __future__ import annotations

from skills.finance.lib.settings import settings

RAJAT_USER_ID: str = "00000000-0000-0000-0000-000000000001"
AYUSHI_USER_ID: str = "00000000-0000-0000-0000-000000000002"


def user_id_for_chat(chat_id: str | int) -> str | None:
    """Return the DB user_id for a Telegram chat id, or None if not whitelisted.

    An empty configured chat id ("") never matches, so an un-provisioned
    Ayushi id cannot accidentally authorize an empty/unknown sender.
    """
    cid = str(chat_id)
    rajat = str(settings.telegram_chat_id_rajat)
    ayushi = str(settings.telegram_chat_id_ayushi)
    if rajat and cid == rajat:
        return RAJAT_USER_ID
    if ayushi and cid == ayushi:
        return AYUSHI_USER_ID
    return None
