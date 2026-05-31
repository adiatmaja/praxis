"""Database module tests."""

from __future__ import annotations

import pytest

from orchestrator.database import Database


@pytest.mark.integration
async def test_initialize_creates_expected_tables(db: Database) -> None:
    rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    table_names = {row["name"] for row in rows}

    assert {"users", "projects", "plans", "tasks", "agent_runs", "opus_state"}.issubset(
        table_names
    )


@pytest.mark.integration
async def test_initialize_seeds_opus_state_row(db: Database) -> None:
    row = await db.fetch_one("SELECT id, status FROM opus_state WHERE id = 1")

    assert row is not None
    assert row["id"] == 1
    assert row["status"] == "available"


@pytest.mark.integration
async def test_initialize_is_idempotent(db: Database) -> None:
    await db.initialize()

    rows = await db.fetch_all("SELECT id FROM opus_state WHERE id = 1")

    assert len(rows) == 1


@pytest.mark.integration
async def test_execute_and_fetch_one_returns_user_row(db: Database) -> None:
    await db.execute(
        "INSERT INTO users (name, token_hash) VALUES (?, ?)",
        ("alice", "hashed-token"),
    )

    row = await db.fetch_one(
        "SELECT name, token_hash FROM users WHERE name = ?",
        ("alice",),
    )

    assert row is not None
    assert row["name"] == "alice"
    assert row["token_hash"] == "hashed-token"


@pytest.mark.integration
async def test_fetch_all_returns_ordered_rows(db: Database) -> None:
    await db.execute(
        "INSERT INTO users (name, token_hash) VALUES (?, ?)",
        ("charlie", "token-c"),
    )
    await db.execute(
        "INSERT INTO users (name, token_hash) VALUES (?, ?)",
        ("bob", "token-b"),
    )

    rows = await db.fetch_all("SELECT name FROM users ORDER BY name ASC")

    assert [row["name"] for row in rows] == ["bob", "charlie"]


@pytest.mark.integration
async def test_fetch_one_returns_none_for_missing_row(db: Database) -> None:
    row = await db.fetch_one("SELECT * FROM users WHERE name = ?", ("nobody",))

    assert row is None
