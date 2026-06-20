"""Tests for /api/docs endpoints."""

from __future__ import annotations


def test_list_docs_filtered(client, auth_headers, db_with_docs):
    import asyncio

    async def _run():
        r = await client.get("/api/docs?category=plan", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert all(d["category"] == "plan" for d in data)

    asyncio.get_event_loop().run_until_complete(_run())


def test_refresh_docs(client, auth_headers, db_with_docs):
    import asyncio

    async def _run():
        r = await client.post("/api/docs/refresh", headers=auth_headers)
        assert r.status_code == 200
        assert "scanned" in r.json()

    asyncio.get_event_loop().run_until_complete(_run())
