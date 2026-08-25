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


@pytest.mark.unit
async def test_migration_7_adds_the_triage_columns(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm7.db'}")
    await db.initialize()
    rows = await db.fetch_all("PRAGMA table_info(tasks)")
    cols = {row["name"] for row in rows}
    for column in (
        "parent_task_id",
        "difficulty_score",
        "leaf_type",
        "triage_decision",
        "escalation_index",
        "implement_harness",
        "implement_model",
    ):
        assert column in cols, f"tasks.{column} missing after migration 7"
    await db.close()


@pytest.mark.unit
async def test_migration_7_is_idempotent(tmp_path):
    # A real re-application of migration 7, not a no-op second `initialize()`:
    # `initialize()` skips migrations whose version is already <= current, so
    # rewinding user_version below 7 forces the ALTER TABLE guard to actually
    # run twice against a table that already has the columns.
    path = tmp_path / "m7-idem.db"
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.initialize()
    await db.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
    await db.close()

    db2 = Database(f"sqlite+aiosqlite:///{path}")
    await db2.initialize()
    row = await db2.fetch_one("PRAGMA user_version")
    assert row is not None
    assert row["user_version"] == CURRENT_SCHEMA_VERSION
    rows = await db2.fetch_all("PRAGMA table_info(tasks)")
    names = [r["name"] for r in rows]
    assert names.count("parent_task_id") == 1
    await db2.close()


@pytest.mark.unit
async def test_escalation_index_defaults_to_zero(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm7-def.db'}")
    await db.initialize()
    await db.execute("INSERT INTO users (id, name, token_hash) VALUES ('u1', 'U', 'h')")
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url) "
        "VALUES ('proj1', 'u1', 'P', 'https://example.com/repo')"
    )
    await db.execute(
        "INSERT INTO plans (id, project_id, source, status) "
        "VALUES ('p1', 'proj1', 'test', 'active')"
    )
    await db.execute(
        "INSERT INTO tasks (id, plan_id, title, description, branch_name) "
        "VALUES ('t1', 'p1', 'T', 'D', 'agent/t')"
    )
    row = await db.fetch_one("SELECT escalation_index FROM tasks WHERE id = 't1'")
    assert row is not None
    assert row["escalation_index"] == 0
    await db.close()


@pytest.mark.unit
def test_current_schema_version_is_eleven():
    assert CURRENT_SCHEMA_VERSION == 11


@pytest.mark.unit
async def test_migration_adds_plan_attempts_defaulting_to_zero(tmp_path):
    """A plan counts its own planning attempts, starting at none.

    NOT NULL DEFAULT 0 is load-bearing: the bound is ``new count reaches the
    maximum``, so a NULL would make the comparison a TypeError on the very tick
    that is supposed to stop the forever-retry.
    """
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'attempts.db'}")
    await db.initialize()
    await db.execute("INSERT INTO users (id, name, token_hash) VALUES ('u1', 'U', 'h')")
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url) "
        "VALUES ('proj1', 'u1', 'P', 'https://example.com/repo')"
    )
    await db.execute(
        "INSERT INTO plans (id, project_id, status) VALUES ('p1', 'proj1', 'pending')"
    )
    row = await db.fetch_one("SELECT plan_attempts FROM plans WHERE id = 'p1'")
    assert row is not None
    assert row["plan_attempts"] == 0
    await db.close()


@pytest.mark.unit
async def test_migration_11_is_idempotent(tmp_path):
    """Re-applying the step against a table that already has the column.

    A crash between ``apply`` and the version bump replays the step, so the
    ``PRAGMA table_info`` guard has to hold. Rewinding user_version is the only
    way to make it actually run twice: ``initialize()`` skips migrations whose
    version is already current.
    """
    path = tmp_path / "m11-idem.db"
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.initialize()
    await db.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
    await db.close()

    db2 = Database(f"sqlite+aiosqlite:///{path}")
    await db2.initialize()
    row = await db2.fetch_one("PRAGMA user_version")
    assert row is not None
    assert row["user_version"] == CURRENT_SCHEMA_VERSION
    names = [r["name"] for r in await db2.fetch_all("PRAGMA table_info(plans)")]
    assert names.count("plan_attempts") == 1
    await db2.close()


@pytest.mark.unit
async def test_migration_adds_review_base_sha_defaulting_to_null(tmp_path):
    """A task can record where its own work starts on a shared branch.

    NULL is the value every pre-existing row has and it is load-bearing: it
    means "review the whole pull request", which is what the loop did before
    this column existed. A NOT NULL column or a non-NULL default would silently
    re-scope every historical row's review.
    """
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'basesha.db'}")
    await db.initialize()
    await db.execute("INSERT INTO users (id, name, token_hash) VALUES ('u1', 'U', 'h')")
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url) "
        "VALUES ('proj1', 'u1', 'P', 'https://example.com/repo')"
    )
    await db.execute(
        "INSERT INTO plans (id, project_id, status) VALUES ('p1', 'proj1', 'pending')"
    )
    await db.execute(
        "INSERT INTO tasks (id, plan_id, title, description, branch_name) "
        "VALUES ('t1', 'p1', 'T', 'D', 'agent/t')"
    )
    row = await db.fetch_one("SELECT review_base_sha FROM tasks WHERE id = 't1'")
    assert row is not None
    assert row["review_base_sha"] is None
    await db.close()


@pytest.mark.unit
async def test_migration_adds_plan_integration_columns(tmp_path):
    """A plan must be able to record where its work went and whether it landed."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'integration.db'}")
    await db.initialize()
    await db.execute("INSERT INTO users (id, name, token_hash) VALUES ('u1', 'U', 'h')")
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url) "
        "VALUES ('proj1', 'u1', 'P', 'https://example.com/repo')"
    )
    await db.execute(
        "INSERT INTO plans (id, project_id, source, status, integration_pr_url) "
        "VALUES ('p1', 'proj1', 'test', 'completed', 'https://x/pull/1')"
    )
    row = await db.fetch_one(
        "SELECT integration_pr_url, integration_merged_at FROM plans WHERE id = 'p1'"
    )
    assert row is not None
    assert row["integration_pr_url"] == "https://x/pull/1"
    assert row["integration_merged_at"] is None
    await db.close()
