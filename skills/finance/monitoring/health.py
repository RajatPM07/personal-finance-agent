"""FastAPI `/health` endpoint.

Returns `{"status": "ok", "db": "reachable"}` when the service can reach
Supabase, otherwise `{"status": "degraded", "db": "error: ..."}`. Errors are
caught and reported in the body — the endpoint itself does not raise so
external monitors (curl, launchd ping checks) can scrape a 200 response and
parse `status` from JSON.

Note: until the schema migration is applied, the `users` table does not exist
yet, so a live request returns `degraded`. That is the intended behavior — the
endpoint surfaces the failure rather than hiding it.
"""
from __future__ import annotations

from fastapi import FastAPI

from skills.finance.lib.db import adb, service_client

app = FastAPI()


@app.get("/health")
async def health() -> dict:
    try:
        await adb(lambda: service_client().table("users").select("id").limit(1).execute())
        return {"status": "ok", "db": "reachable"}
    except Exception as e:
        return {"status": "degraded", "db": f"error: {e}"}
