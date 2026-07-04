"""Tests for the morning-brief data-fetch layer. DB access fully mocked."""
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import skills.finance.nudging.morning_brief as mb


@pytest.fixture
def fake_conn(monkeypatch):
    """Patches readonly_client() with a MagicMock whose cursor returns
    scripted results per query, in call order."""
    conn = MagicMock()
    monkeypatch.setattr(mb, "readonly_client", lambda: conn)
    return conn


def _script_queries(conn, results: list[list[tuple]]):
    """Each execute() call pops the next result set."""
    cursors = []
    for rows in results:
        cur = MagicMock()
        cur.fetchall.return_value = rows
        cur.fetchone.return_value = rows[0] if rows else None
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cursors.append(cur)
    conn.cursor.side_effect = cursors


WATERMARK = datetime(2026, 7, 3, 3, 30, tzinfo=UTC)


class TestFetchBriefData:
    def test_assembles_brief_data(self, fake_conn):
        _script_queries(fake_conn, [
            # 1. new txns since watermark (merchant, amount, category)
            [("Amazon", Decimal("3180"), "Shopping"),
             ("Swiggy", Decimal("640"), "Food Delivery")],
            # 2. MTD total
            [(Decimal("38400"),)],
            # 3. monthly totals for prior full months (month, total)
            [(datetime(2026, 1, 1), Decimal("250000")),
             (datetime(2026, 2, 1), Decimal("270000"))],
            # 4. MTD by category
            [("Dining Out", Decimal("9200"))],
            # 5. category monthly avg over prior full months
            [("Dining Out", Decimal("22575"))],
        ])
        data = mb.fetch_brief_data(WATERMARK)
        assert [t.merchant for t in data.new_txns] == ["Amazon", "Swiggy"]
        assert data.mtd_total == Decimal("38400")
        assert data.monthly_avg == Decimal("260000")
        assert data.months_of_history == 2
        assert data.top_category == "Dining Out"

    def test_empty_db_yields_zeroes(self, fake_conn):
        _script_queries(fake_conn, [[], [(None,)], [], [], []])
        data = mb.fetch_brief_data(WATERMARK)
        assert data.new_txns == []
        assert data.mtd_total == Decimal("0")
        assert data.monthly_avg == Decimal("0")
        assert data.months_of_history == 0
        assert data.top_category is None

    def test_new_txn_null_category_preserved(self, fake_conn):
        _script_queries(fake_conn, [
            [("Mystery", Decimal("500"), None)],
            [(Decimal("500"),)], [], [], [],
        ])
        data = mb.fetch_brief_data(WATERMARK)
        assert data.new_txns[0].category is None


class TestWatermark:
    @pytest.mark.asyncio
    async def test_get_watermark_returns_stored_ts(self, monkeypatch):
        stored = {"ts": "2026-07-03T03:30:00+00:00"}
        resp = MagicMock()
        resp.data = [{"value": stored}]
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = resp
        monkeypatch.setattr(mb, "service_client", lambda: client)
        ts = await mb.get_watermark()
        assert ts == datetime(2026, 7, 3, 3, 30, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_get_watermark_missing_defaults_24h(self, monkeypatch):
        resp = MagicMock()
        resp.data = []
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = resp
        monkeypatch.setattr(mb, "service_client", lambda: client)
        before = datetime.now(tz=UTC)
        ts = await mb.get_watermark()
        assert (before - ts).total_seconds() == pytest.approx(24 * 3600, abs=120)

    @pytest.mark.asyncio
    async def test_set_watermark_upserts_and_executes(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(mb, "service_client", lambda: client)
        ts = datetime(2026, 7, 4, 3, 30, tzinfo=UTC)
        await mb.set_watermark(ts)
        upsert_call = client.table.return_value.upsert
        assert upsert_call.called
        payload = upsert_call.call_args.args[0]
        assert payload["key"] == "morning_brief_last_run"
        assert payload["value"] == {"ts": "2026-07-04T03:30:00+00:00"}
        # invariant #2: chain terminated with .execute()
        assert upsert_call.return_value.execute.called
