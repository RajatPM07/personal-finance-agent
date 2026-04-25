import asyncio


def test_service_client_exposes_handle():
    from skills.finance.lib.db import service_client
    assert service_client() is not None


def test_adb_runs_sync_fn_in_thread():
    """adb() must be the single enforcement point for async-to-sync bridging."""
    from skills.finance.lib.db import adb
    called_with = {}

    def sync_fn(x, y=None):
        called_with["x"] = x
        called_with["y"] = y
        return "result"

    out = asyncio.run(adb(sync_fn, 42, y="hi"))
    assert out == "result"
    assert called_with == {"x": 42, "y": "hi"}
