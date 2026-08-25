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
    # A real re-application of migration 7. `initialize()` skips migrations at
    # or below the stored version, and the rewind this used to do
    # (`CURRENT_SCHEMA_VERSION - 1`) has not reached migration 7 since migration
    # 8 landed - so the step never re-ran and the surviving assertion passed
    # either way. Invoking the step directly is what makes the guard testable:
    # delete the `PRAGMA table_info` check and the second call raises
    # `duplicate column name`.
    from orchestrator.database import _migration_0007_leaf_triage

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm7-idem.db'}")
    await db.initialize()
    # try/finally for the reason given in ``test_migration_12_is_idempotent``:
    # without it the regression case hangs rather than failing.
    try:
        connection = db._connection
        assert connection is not None
        await _migration_0007_leaf_triage(connection)
        await _migration_0007_leaf_triage(connection)
        names = [r["name"] for r in await db.fetch_all("PRAGMA table_info(tasks)")]
        assert names.count("parent_task_id") == 1
    finally:
        await db.close()


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
def test_current_schema_version_is_twelve():
    assert CURRENT_SCHEMA_VERSION == 12


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
    ``PRAGMA table_info`` guard has to hold.

    Invoked directly, for the reason spelled out in
    ``test_migration_12_is_idempotent``: rewinding ``user_version`` by
    ``CURRENT_SCHEMA_VERSION - 1`` stopped reaching this migration when 12
    landed, and the assertion left behind passes whether the step re-ran or
    never ran.
    """
    from orchestrator.database import _migration_0011_plan_attempts

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm11-idem.db'}")
    await db.initialize()
    # try/finally for the reason given in ``test_migration_12_is_idempotent``:
    # without it the regression case hangs rather than failing.
    try:
        connection = db._connection
        assert connection is not None
        await _migration_0011_plan_attempts(connection)
        await _migration_0011_plan_attempts(connection)
        names = [r["name"] for r in await db.fetch_all("PRAGMA table_info(plans)")]
        assert names.count("plan_attempts") == 1
    finally:
        await db.close()


@pytest.mark.unit
async def test_migration_adds_project_context_window_defaulting_to_null(tmp_path):
    """A project can declare its worker's context window; NULL means undeclared.

    NULL is load-bearing: it means "fall through to the settings file's
    declaration, then to the LM Studio probe, then to unknown". A NOT NULL
    column with any default would put a fabricated window back on every project
    row, which is the entire defect this column exists to close.
    """
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'ctxwin.db'}")
    await db.initialize()
    await db.execute("INSERT INTO users (id, name, token_hash) VALUES ('u1', 'U', 'h')")
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url) "
        "VALUES ('proj1', 'u1', 'P', 'https://example.com/repo')"
    )
    row = await db.fetch_one("SELECT context_window FROM projects WHERE id = 'proj1'")
    assert row is not None
    assert row["context_window"] is None
    await db.close()


@pytest.mark.unit
async def test_migration_12_is_idempotent(tmp_path):
    """Re-applying the step against a table that already has the column.

    A crash between ``apply`` and the version bump replays the step, so the
    ``PRAGMA table_info`` guard has to hold.

    The step is invoked DIRECTLY, twice, rather than by rewinding
    ``user_version``. The rewind is only a proxy for "the step ran again", and
    it is a proxy that breaks silently: ``CURRENT_SCHEMA_VERSION - 1`` stopped
    rewinding past this migration the moment a later one was added, and the
    surviving assertion (``count(...) == 1``) cannot tell a re-applied guard
    from a migration that never ran at all. Both forms of this test were
    therefore green whether the guard existed or not. Calling the function is
    the only version of this that fails when the guard is deleted: without it
    the second call raises ``duplicate column name``.
    """
    from orchestrator.database import _migration_0012_project_context_window

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm12-idem.db'}")
    await db.initialize()
    # try/finally, because without it a FAILING version of this test HANGS
    # instead of failing: the second call raises, `close()` never runs, and the
    # aiosqlite worker thread keeps the loop alive until the suite times out.
    # A test that wedges on regression is barely better than one that cannot
    # fail - CI reports a timeout, not the guard that went missing.
    try:
        connection = db._connection
        assert connection is not None
        await _migration_0012_project_context_window(connection)
        await _migration_0012_project_context_window(connection)
        names = [r["name"] for r in await db.fetch_all("PRAGMA table_info(projects)")]
        assert names.count("context_window") == 1
    finally:
        await db.close()


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
