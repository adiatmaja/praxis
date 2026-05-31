"""Database module tests."""
# ruff: noqa: S101, S105

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
        INSERT INTO plans (id, project_id, spec)
        VALUES (?, ?, ?)
        """,
        ("plan-1", "project-1", "Build plan"),
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
