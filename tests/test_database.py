"""Database module tests."""
# ruff: noqa: S101, S105

from __future__ import annotations

import sqlite3

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
async def test_domain_table_id_columns_are_text_and_required_relationships(
    db: Database,
) -> None:
    users_info = await db.fetch_all("PRAGMA table_info(users)")
    projects_info = await db.fetch_all("PRAGMA table_info(projects)")
    plans_info = await db.fetch_all("PRAGMA table_info(plans)")
    tasks_info = await db.fetch_all("PRAGMA table_info(tasks)")
    agent_runs_info = await db.fetch_all("PRAGMA table_info(agent_runs)")

    users_columns = {column["name"]: column for column in users_info}
    projects_columns = {column["name"]: column for column in projects_info}
    plans_columns = {column["name"]: column for column in plans_info}
    tasks_columns = {column["name"]: column for column in tasks_info}
    agent_runs_columns = {column["name"]: column for column in agent_runs_info}

    assert users_columns["id"]["type"] == "TEXT"
    assert users_columns["id"]["pk"] == 1
    assert projects_columns["id"]["type"] == "TEXT"
    assert projects_columns["user_id"]["type"] == "TEXT"
    assert plans_columns["id"]["type"] == "TEXT"
    assert plans_columns["project_id"]["type"] == "TEXT"
    assert tasks_columns["id"]["type"] == "TEXT"
    assert tasks_columns["plan_id"]["type"] == "TEXT"
    assert tasks_columns["branch_name"]["notnull"] == 1
    assert agent_runs_columns["id"]["type"] == "TEXT"
    assert agent_runs_columns["task_id"]["type"] == "TEXT"
    assert agent_runs_columns["container_id"]["notnull"] == 1


@pytest.mark.integration
async def test_initialize_seeds_opus_state_row(db: Database) -> None:
    row = await db.fetch_one("SELECT id, status FROM opus_state WHERE id = 1")

    assert row is not None
    assert row["id"] == 1
    assert row["status"] == "available"


@pytest.mark.integration
async def test_opus_state_rejects_non_singleton_id(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO opus_state (id, status, queued_actions) VALUES (?, ?, ?)",
            (2, "available", "[]"),
        )


@pytest.mark.integration
async def test_initialize_is_idempotent(db: Database) -> None:
    await db.initialize()

    rows = await db.fetch_all("SELECT id FROM opus_state WHERE id = 1")

    assert len(rows) == 1


@pytest.mark.integration
async def test_execute_and_fetch_one_returns_user_row(db: Database) -> None:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("user-1", "alice", "hashed-token"),
    )

    row = await db.fetch_one(
        "SELECT id, name, token_hash FROM users WHERE id = ?",
        ("user-1",),
    )

    assert row is not None
    assert row["id"] == "user-1"
    assert row["name"] == "alice"
    assert row["token_hash"] == "hashed-token"


@pytest.mark.integration
async def test_fetch_all_returns_ordered_rows(db: Database) -> None:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("user-c", "charlie", "token-c"),
    )
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("user-b", "bob", "token-b"),
    )

    rows = await db.fetch_all("SELECT name FROM users ORDER BY name ASC")

    assert [row["name"] for row in rows] == ["bob", "charlie"]


@pytest.mark.integration
async def test_fetch_one_returns_none_for_missing_row(db: Database) -> None:
    row = await db.fetch_one("SELECT * FROM users WHERE name = ?", ("nobody",))

    assert row is None


@pytest.mark.integration
async def test_doc_index_table_exists(db: Database) -> None:
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='doc_index'"
    )
    assert len(rows) == 1


@pytest.mark.integration
async def test_projects_have_agent_model_columns(db: Database) -> None:
    cols = [r["name"] for r in await db.fetch_all("PRAGMA table_info(projects)")]
    assert "agent_model" in cols
    assert "agent_model_effort" in cols


@pytest.mark.unit
async def test_projects_table_has_harness_column(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
    await db.initialize()
    try:
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "admin", "h"),
        )
        import uuid

        pid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO projects (id, user_id, name, repo_url, model_name, "
            "harness) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, "u1", "p", "https://x/y", "m", "opencode"),
        )
        row = await db.fetch_one("SELECT harness FROM projects WHERE id = ?", (pid,))
        assert row is not None
        assert row["harness"] == "opencode"
    finally:
        await db.close()


@pytest.mark.unit
async def test_harness_defaults_to_opencode(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mig2.db'}")
    await db.initialize()
    try:
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "admin", "h"),
        )
        import uuid

        pid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, "u1", "p", "https://x/y", "m"),
        )
        row = await db.fetch_one("SELECT harness FROM projects WHERE id = ?", (pid,))
        assert row is not None
        assert row["harness"] == "opencode"
    finally:
        await db.close()


@pytest.mark.integration
async def test_string_ids_work_across_related_tables(db: Database) -> None:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("user-1", "alice", "hashed-token"),
    )
    await db.execute(
        """
        INSERT INTO projects (
            id, user_id, name, repo_url, model_name
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            "project-1",
            "user-1",
            "Praxis",
            "https://github.com/adiatmaja/praxis.git",
            "qwen2.5-coder",
        ),
    )
    await db.execute(
        """
        INSERT INTO plans (id, project_id)
        VALUES (?, ?)
        """,
        ("plan-1", "project-1"),
    )
    await db.execute(
        """
        INSERT INTO tasks (id, plan_id, title, description, branch_name)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("task-1", "plan-1", "Task", "Do work", "agent/task-1"),
    )
    await db.execute(
        """
        INSERT INTO agent_runs (id, task_id, container_id)
        VALUES (?, ?, ?)
        """,
        ("run-1", "task-1", "container-1"),
    )

    row = await db.fetch_one(
        "SELECT id, task_id, container_id FROM agent_runs WHERE id = ?",
        ("run-1",),
    )

    assert row is not None
    assert row["id"] == "run-1"
    assert row["task_id"] == "task-1"
    assert row["container_id"] == "container-1"


@pytest.mark.integration
async def test_plans_has_path_columns(db: Database) -> None:
    rows = await db.fetch_all("PRAGMA table_info(plans)")
    names = {r["name"] for r in rows}
    assert "spec_path" in names
    assert "plan_path" in names


@pytest.mark.integration
async def test_plans_spec_column_dropped(db: Database) -> None:
    rows = await db.fetch_all("PRAGMA table_info(plans)")
    names = {r["name"] for r in rows}
    assert "spec" not in names
    assert "spec_path" in names
    assert "plan_path" in names
    assert "opus_plan" in names  # retained: runtime task graph


@pytest.mark.integration
async def test_tasks_table_has_escalation_columns(db: Database) -> None:
    cols = {
        row[1]
        for row in await (await db.execute("PRAGMA table_info(tasks)")).fetchall()
    }
    assert {"needs_stronger_model", "escalation_state", "escalated_to"} <= cols


@pytest.mark.integration
async def test_tasks_table_has_checklist_and_progress_note(db: Database) -> None:
    cols = {r["name"] for r in await db.fetch_all("PRAGMA table_info(tasks)")}
    assert "checklist" in cols
    assert "progress_note" in cols


@pytest.mark.asyncio
async def test_new_columns_exist(tmp_path) -> None:
    """projects.auto_merge and tasks.approved_at are present after initialize."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await db.initialize()
    try:
        proj_cols = {
            row["name"] for row in await db.fetch_all("PRAGMA table_info(projects)")
        }
        task_cols = {
            row["name"] for row in await db.fetch_all("PRAGMA table_info(tasks)")
        }
        assert "auto_merge" in proj_cols
        assert "approved_at" in task_cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_verify_cmd_column_exists(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'v.db'}")
    await db.initialize()
    try:
        cols = {
            row["name"] for row in await db.fetch_all("PRAGMA table_info(projects)")
        }
        assert "verify_cmd" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_tasks_table_has_clarification_columns(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    await db.initialize()
    try:
        cols = {
            row["name"] for row in await db.fetch_all("PRAGMA table_info(tasks)")
        }
        assert {"clarification_question", "clarification_answer", "clarification_state"} <= cols
    finally:
        await db.close()
