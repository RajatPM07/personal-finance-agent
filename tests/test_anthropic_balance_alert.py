"""W4.1 §4.4 + §7: scheduled job that alerts on Anthropic balance < $3.
Path A (API) or Path B (logs-derived) — both exposed through the same
async function `check_anthropic_balance()`."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_alert_fires_below_threshold():
    """When balance < anthropic_balance_warning_usd, send_alert is called."""
    from skills.finance.monitoring import alerts

    with patch.object(alerts, "_fetch_anthropic_balance_usd", return_value=2.50), \
         patch.object(alerts, "send_alert", new=AsyncMock()) as m_send:
        await alerts.check_anthropic_balance()

    m_send.assert_called_once()
    sent_text = m_send.call_args[0][0]
    assert "2.5" in sent_text or "2.50" in sent_text
    assert "anthropic" in sent_text.lower() or "balance" in sent_text.lower()


@pytest.mark.asyncio
async def test_no_alert_above_threshold():
    from skills.finance.monitoring import alerts

    with patch.object(alerts, "_fetch_anthropic_balance_usd", return_value=4.50), \
         patch.object(alerts, "send_alert", new=AsyncMock()) as m_send:
        await alerts.check_anthropic_balance()

    m_send.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_failure_alerts_once_then_continues():
    """If the fetch fails (API down, auth issue), alert that we couldn't
    check — don't crash the scheduler."""
    from skills.finance.monitoring import alerts

    with patch.object(alerts, "_fetch_anthropic_balance_usd", side_effect=RuntimeError("boom")), \
         patch.object(alerts, "send_alert", new=AsyncMock()) as m_send:
        await alerts.check_anthropic_balance()

    m_send.assert_called_once()
    assert "couldn't" in m_send.call_args[0][0].lower() or "failed" in m_send.call_args[0][0].lower()


def test_fetch_balance_queries_total_cost_and_claude_models():
    """Regression: the live request_logs table stores per-call USD in
    `total_cost` and logs Anthropic models as `claude-*`. Selecting
    `response_cost` raised 42703 (daily failure); filtering `%anthropic%`
    matched no rows and silently reported a permanent full balance. This
    test exercises the real query construction — mocking only the client —
    so either regression would fail it."""
    from skills.finance.monitoring import alerts

    fake_result = MagicMock()
    fake_result.data = [{"total_cost": 0.10}, {"total_cost": 0.15}, {"total_cost": None}]
    builder = MagicMock()
    builder.select.return_value = builder
    builder.ilike.return_value = builder
    builder.execute.return_value = fake_result
    client = MagicMock()
    client.table.return_value = builder

    with patch.object(alerts, "service_client", return_value=client):
        balance = alerts._fetch_anthropic_balance_usd()

    client.table.assert_called_once_with("request_logs")
    builder.select.assert_called_once_with("total_cost")
    builder.ilike.assert_called_once_with("model", "%claude%")
    # $5.00 starting credit minus (0.10 + 0.15 + 0) spent.
    assert balance == pytest.approx(4.75)
