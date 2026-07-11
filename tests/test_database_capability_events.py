"""Tests for the capability_events migration (Migration 4)."""

from __future__ import annotations

import pytest

from orchestrator.database import CURRENT_SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_capability_events_table_exists(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cap.db'}")
    await db.initialize()
    try:
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='capability_events'"
        )
        assert len(rows) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schema_version_is_at_least_4(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cap2.db'}")
    await db.initialize()
    try:
        assert CURRENT_SCHEMA_VERSION >= 4

        row = await db.fetch_one("PRAGMA user_version")
        assert row is not None
        assert row["user_version"] == CURRENT_SCHEMA_VERSION
        assert row["user_version"] >= 4
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_capability_events_columns(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cap3.db'}")
    await db.initialize()
    try:
        rows = await db.fetch_all("PRAGMA table_info(capability_events)")
        column_names = {r["name"] for r in rows}
        assert column_names == {
            "id",
            "schema_version",
            "event_type",
            "plan_id",
            "task_id",
            "payload",
            "created_at",
        }
    finally:
        await db.close()
