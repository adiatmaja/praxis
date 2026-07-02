"""Versioned-migration framework tests (PRAGMA user_version based)."""
# ruff: noqa: S101

import pytest

from orchestrator.database import CURRENT_SCHEMA_VERSION, MIGRATIONS, Database


@pytest.mark.unit
async def test_fresh_db_lands_on_current_version(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    await db.initialize()
    row = await db.fetch_one("PRAGMA user_version")
    assert row is not None
    assert row["user_version"] == CURRENT_SCHEMA_VERSION
    await db.close()


@pytest.mark.unit
async def test_initialize_is_idempotent(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}")
    await db.initialize()
    await db.close()
    db2 = Database(f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}")
    await db2.initialize()
    row = await db2.fetch_one("PRAGMA user_version")
    assert row is not None
    assert row["user_version"] == CURRENT_SCHEMA_VERSION
    await db2.close()


@pytest.mark.unit
async def test_pending_migration_applies_in_order(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
    await db.initialize()
    await db.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
    await db.close()

    db2 = Database(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
    await db2.initialize()
    row = await db2.fetch_one("PRAGMA user_version")
    assert row is not None
    assert row["user_version"] == CURRENT_SCHEMA_VERSION
    await db2.close()


@pytest.mark.unit
def test_migrations_are_contiguous_from_one():
    versions = [m.version for m in MIGRATIONS]
    assert versions == list(range(1, CURRENT_SCHEMA_VERSION + 1))
