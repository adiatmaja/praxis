"""Tests for the task_outcomes migration (Migration 5)."""

from __future__ import annotations

import pytest

from orchestrator.database import CURRENT_SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_task_outcomes_table_exists(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'to.db'}")
    await db.initialize()
    try:
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_outcomes'"
        )
        assert len(rows) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schema_version_is_at_least_5(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'to2.db'}")
    await db.initialize()
    try:
        assert CURRENT_SCHEMA_VERSION >= 5

        row = await db.fetch_one("PRAGMA user_version")
        assert row is not None
        assert row["user_version"] == CURRENT_SCHEMA_VERSION
        assert row["user_version"] >= 5
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_task_outcomes_columns(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'to3.db'}")
    await db.initialize()
    try:
        rows = await db.fetch_all("PRAGMA table_info(task_outcomes)")
        column_names = {r["name"] for r in rows}
        assert column_names == {
            "id",
            "task_id",
            "project_id",
            "model_name",
            "harness",
            "task_type",
            "files_touched",
            "loc_delta",
            "context_tokens_est",
            "attempt",
            "outcome",
            "failure_class",
            "split_depth",
            "source",
            "created_at",
        }
    finally:
        await db.close()
