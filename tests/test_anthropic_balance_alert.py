"""W4.1 §4.4 + §7: scheduled job that alerts on Anthropic balance < $3.
Path A (API) or Path B (logs-derived) — both exposed through the same
async function `check_anthropic_balance()`."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
