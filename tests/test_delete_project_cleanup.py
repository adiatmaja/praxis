"""DELETE /api/projects/{id} must not 500 on a project with children.

Defect: the handler ran a bare ``DELETE FROM projects WHERE id = ?`` with no
regard for ``plans``/``tasks``/``agent_runs`` rows that reference the project
(directly or transitively). ``Database.initialize()`` turns
``PRAGMA foreign_keys=ON``, so any of those rows makes the delete raise
``sqlite3.IntegrityError: FOREIGN KEY constraint failed``, which FastAPI
turns into a bare 500 with no indication of what is still attached or what
to do about it. A newcomer cleaning up a throwaway project hits this on the
very first non-empty project they try to remove.

Fix: cascade the delete inside the endpoint, removing agent_runs, tasks, and
plans that belong to the project before removing the project row itself, in
dependency order (leaf tables first) so no intermediate step trips the same
constraint.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


async def _seed_project_with_full_tree(db: Database) -> tuple[str, str, str, str]:
    """Seed a project with one plan, one task, and one agent_run under it.

    Returns (project_id, plan_id, task_id, agent_run_id).
    """
    user_id = await seed_user(db)
    project_id = "proj-cleanup-1"
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, model_name, harness)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            user_id,
            "CleanupProj",
            "https://github.com/o/cleanup-repo",
            "main",
            "qwen3.6-27b",
            "opencode",
        ),
    )
    plan_id = "plan-cleanup-1"
    await db.execute(
        """INSERT INTO plans (id, project_id, status) VALUES (?, ?, ?)""",
        (plan_id, project_id, "pending"),
    )
    task_id = "task-cleanup-1"
    await db.execute(
        """INSERT INTO tasks (id, plan_id, title, description, branch_name)
           VALUES (?, ?, ?, ?, ?)""",
        (task_id, plan_id, "Do the thing", "Do the thing", "agent/do-the-thing"),
    )
    agent_run_id = "run-cleanup-1"
    await db.execute(
        """INSERT INTO agent_runs (id, task_id, container_id) VALUES (?, ?, ?)""",
        (agent_run_id, task_id, "container-1"),
    )
    return project_id, plan_id, task_id, agent_run_id


@pytest.mark.integration
async def test_delete_project_with_children_reproduces_fk_error_before_fix(
    db: Database,
) -> None:
    """Direct-DB reproduction of the real sqlite error, independent of the fix.

    This pins the ACTUAL failure mode named in the defect report: not a
    generic error, specifically a foreign key constraint violation, so a fix
    that papers over a different symptom does not accidentally satisfy this
    test.
    """
    import aiosqlite

    project_id, _plan_id, _task_id, _run_id = await _seed_project_with_full_tree(db)

    with pytest.raises(aiosqlite.IntegrityError, match="FOREIGN KEY constraint failed"):
        await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@pytest.mark.integration
async def test_delete_project_with_children_does_not_500(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The REST endpoint must not surface a bare 500 for a non-empty project."""
    project_id, _plan_id, _task_id, _run_id = await _seed_project_with_full_tree(db)

    resp = await client.delete(f"/api/projects/{project_id}", headers=auth_headers)

    assert resp.status_code != 500


@pytest.mark.integration
async def test_delete_project_with_children_cascades_all_rows(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """Cascading delete: the project and every row that referenced it is gone."""
    project_id, plan_id, task_id, agent_run_id = await _seed_project_with_full_tree(db)

    resp = await client.delete(f"/api/projects/{project_id}", headers=auth_headers)
    assert resp.status_code == 204

    assert (
        await db.fetch_one("SELECT id FROM projects WHERE id = ?", (project_id,))
        is None
    )
    assert await db.fetch_one("SELECT id FROM plans WHERE id = ?", (plan_id,)) is None
    assert await db.fetch_one("SELECT id FROM tasks WHERE id = ?", (task_id,)) is None
    assert (
        await db.fetch_one("SELECT id FROM agent_runs WHERE id = ?", (agent_run_id,))
        is None
    )


@pytest.mark.integration
async def test_delete_project_with_children_leaves_other_projects_alone(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A cascade must be scoped to the deleted project, not global."""
    project_id, _plan_id, _task_id, _run_id = await _seed_project_with_full_tree(db)

    other_project_id = "proj-cleanup-other"
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, model_name, harness)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            other_project_id,
            "test-user",
            "OtherProj",
            "https://github.com/o/other-repo",
            "main",
            "qwen3.6-27b",
            "opencode",
        ),
    )
    other_plan_id = "plan-cleanup-other"
    await db.execute(
        """INSERT INTO plans (id, project_id, status) VALUES (?, ?, ?)""",
        (other_plan_id, other_project_id, "pending"),
    )

    resp = await client.delete(f"/api/projects/{project_id}", headers=auth_headers)
    assert resp.status_code == 204

    assert (
        await db.fetch_one("SELECT id FROM projects WHERE id = ?", (other_project_id,))
        is not None
    )
    assert (
        await db.fetch_one("SELECT id FROM plans WHERE id = ?", (other_plan_id,))
        is not None
    )
