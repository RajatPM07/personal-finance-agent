"""W4.1 /ask command handler — wires the SQL agent to the aiogram bot.

We mock run_sql_agent and the message-send call so the test is offline.
The whitelist gate (_authorized_user_id) resolves the chat id to a DB
user_id (Task 3); we patch it to authorize/reject the fake message."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skills.finance.agents.judge import JudgeVerdict
from skills.finance.agents.sql_agent import AgentResult
from skills.finance.agents.sql_validator import ValidationResult
from skills.finance.lib.users import RAJAT_USER_ID


@pytest.mark.asyncio
async def test_ask_handler_renders_success():
    """Successful agent run → bot replies with the answer (the ₹-formatted
    count). SQL is intentionally not surfaced to the user."""
    from skills.finance.bot.main import cmd_ask

    msg = MagicMock()
    msg.from_user.id = 12345
    msg.text = "/ask How many transactions total?"
    msg.answer = AsyncMock()

    fake_result = AgentResult(
        final="rendered",
        sql="SELECT count(*) FROM transactions",
        rows=[{"count": 1227}],
        judge_verdict=JudgeVerdict(verdict="ok", confidence=0.95, reason="ok"),
        validator_result=ValidationResult(ok=True, statement_type="select"),
        escalated=False,
        retried=False,
        reason=None,
    )

    with patch("skills.finance.bot.main._authorized_user_id", return_value=RAJAT_USER_ID), \
         patch("skills.finance.bot.main.run_sql_agent_async", return_value=fake_result):
        await cmd_ask(msg)

    msg.answer.assert_called_once()
    sent_text = msg.answer.call_args[0][0]
    assert "1,227" in sent_text  # the count, ₹-formatted by the fast formatter
    assert "SELECT" not in sent_text  # raw SQL is never shown to the user


@pytest.mark.asyncio
async def test_ask_handler_renders_surface_to_user():
    """When agent surfaces, bot relays the rephrase message — no SQL shown."""
    from skills.finance.bot.main import cmd_ask

    msg = MagicMock()
    msg.from_user.id = 12345
    msg.text = "/ask ambiguous question"
    msg.answer = AsyncMock()

    fake_result = AgentResult(
        final="surfaced_to_user",
        sql="SELECT 4 FROM transactions",
        rows=[],
        judge_verdict=JudgeVerdict(verdict="wrong", confidence=0.95, reason="still wrong"),
        validator_result=ValidationResult(ok=True, statement_type="select"),
        escalated=True,
        retried=True,
        reason="I'm not sure how to answer — rephrase?",
    )

    with patch("skills.finance.bot.main._authorized_user_id", return_value=RAJAT_USER_ID), \
         patch("skills.finance.bot.main.run_sql_agent_async", return_value=fake_result):
        await cmd_ask(msg)

    sent_text = msg.answer.call_args[0][0]
    assert "rephrase" in sent_text.lower()
    assert "SELECT 4" not in sent_text  # don't expose the failed SQL


@pytest.mark.asyncio
async def test_ask_handler_whitelist_silently_rejects_non_rajat():
    """Per CLAUDE.md invariant #7, non-whitelisted users get silent return."""
    from skills.finance.bot.main import cmd_ask

    msg = MagicMock()
    msg.from_user.id = 99999
    msg.text = "/ask anything"
    msg.answer = AsyncMock()

    with patch("skills.finance.bot.main._authorized_user_id", return_value=None):
        await cmd_ask(msg)

    msg.answer.assert_not_called()


@pytest.mark.asyncio
async def test_ask_handler_empty_question_replies_with_usage():
    """`/ask` with no question text → terse usage hint, no agent call."""
    from skills.finance.bot.main import cmd_ask

    msg = MagicMock()
    msg.from_user.id = 12345
    msg.text = "/ask"
    msg.answer = AsyncMock()

    with patch("skills.finance.bot.main._authorized_user_id", return_value=RAJAT_USER_ID), \
         patch("skills.finance.bot.main.run_sql_agent_async") as m_agent:
        await cmd_ask(msg)

    m_agent.assert_not_called()
    msg.answer.assert_called_once()
    assert "/ask" in msg.answer.call_args[0][0]
