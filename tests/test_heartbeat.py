from datetime import UTC, datetime, timedelta


def test_stale_components_flagged_when_exceeds_threshold():
    from skills.finance.monitoring.heartbeat import find_stale_components

    now = datetime(2026, 4, 21, 14, 0, tzinfo=UTC)
    latest = {
        "gmail_scanner": {"component": "gmail_scanner", "last_ping": (now - timedelta(minutes=10)).isoformat()},
        "morning_brief": {"component": "morning_brief", "last_ping": (now - timedelta(minutes=45)).isoformat()},
        "telegram_bot": {"component": "telegram_bot", "last_ping": (now - timedelta(minutes=5)).isoformat()},
    }
    stale = find_stale_components(latest.values(), now=now, threshold_minutes=30)
    assert stale == ["morning_brief"]
