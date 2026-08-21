"""Plans API tests."""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


async def _create_project(client: AsyncClient, auth_headers: dict[str, str]) -> str:
    response = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    return str(response.json()["id"])


@pytest.fixture
def spec_repo(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Capture spec docs the API commits, keyed by repo path."""
    written: dict[str, str] = {}

    async def _write_and_commit(repo_url: str, path: str, content: str) -> dict:
        written[path] = content
        return {"status": "committed", "path": path}

    monkeypatch.setattr(
        client.app.state.brainstorm,  # type: ignore[attr-defined]
        "write_and_commit",
        _write_and_commit,
    )
    return written


@pytest.mark.integration
async def test_create_list_get_approve_reject_plan(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    spec_repo: dict[str, str],
) -> None:
    await seed_user(db)
    project_id = await _create_project(client, auth_headers)

    created = await client.post(
        f"/api/projects/{project_id}/plans",
        json={"spec": "Build a login page"},
        headers=auth_headers,
    )
    plan_id = created.json()["id"]
    listed = await client.get(f"/api/projects/{project_id}/plans", headers=auth_headers)
    fetched = await client.get(f"/api/plans/{plan_id}", headers=auth_headers)
    approved = await client.post(f"/api/plans/{plan_id}/approve", headers=auth_headers)
    rejected = await client.post(f"/api/plans/{plan_id}/reject", headers=auth_headers)

    assert created.status_code == 201
    assert created.json()["status"] == "pending"
    assert len(listed.json()) == 1
    assert fetched.status_code == 200
    assert approved.json()["status"] == "active"
    assert rejected.json()["status"] == "rejected"


@pytest.mark.integration
async def test_submitted_spec_is_committed_and_recorded_on_the_plan(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    spec_repo: dict[str, str],
) -> None:
    await seed_user(db)
    project_id = await _create_project(client, auth_headers)

    created = await client.post(
        f"/api/projects/{project_id}/plans",
        json={"spec": "Add rate limiting to the login endpoint"},
        headers=auth_headers,
    )

    assert created.status_code == 201
    row = await db.fetch_one(
        "SELECT spec_path FROM plans WHERE id = ?", (created.json()["id"],)
    )
    assert row is not None
    spec_path = row["spec_path"]
    assert spec_path is not None
    assert spec_path.startswith("docs/superpowers/specs/")
    assert "Add rate limiting to the login endpoint" in spec_repo[spec_path]


@pytest.mark.integration
async def test_create_plan_fails_closed_when_the_spec_cannot_be_persisted(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No plan may exist without its spec: a spec-less plan plans from nothing."""
    await seed_user(db)
    project_id = await _create_project(client, auth_headers)

    async def _boom(repo_url: str, path: str, content: str) -> dict:
        msg = "push rejected"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        client.app.state.brainstorm,  # type: ignore[attr-defined]
        "write_and_commit",
        _boom,
    )

    created = await client.post(
        f"/api/projects/{project_id}/plans",
        json={"spec": "Add rate limiting"},
        headers=auth_headers,
    )

    assert created.status_code == 502
    assert await db.fetch_all("SELECT id FROM plans") == []


@pytest.mark.integration
async def test_plan_not_found(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/plans/missing", headers=auth_headers)

    assert response.status_code == 404


@pytest.mark.integration
async def test_promote_creates_and_activates_run(
    db: Database,
    client: AsyncClient,
    auth_headers: dict[str, str],
    mocker: Any,
) -> None:
    import uuid

    await seed_user(db)
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name, lm_studio_url) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, "test-user", "p", "https://example.com/r.git", "qwen", "http://lm:1234"),
    )
    plan_md = (
        "---\nspec_path: docs/superpowers/specs/x-design.md\n---\n"
        "# X Plan\n\n### Task 1: Do thing\n\nDetails.\n"
    )
    mocker.patch.object(
        client.app.state.brainstorm,
        "read_doc",
        new=mocker.AsyncMock(return_value=plan_md),
    )

    resp = await client.post(
        "/api/plans/promote",
        headers=auth_headers,
        json={"project_id": pid, "plan_path": "docs/superpowers/plans/x.md"},
    )
    assert resp.status_code == 201
    plan = resp.json()
    row = await db.fetch_one("SELECT * FROM plans WHERE id = ?", (plan["id"],))
    assert row["plan_path"] == "docs/superpowers/plans/x.md"
    assert row["spec_path"] == "docs/superpowers/specs/x-design.md"
    assert row["status"] == "active"
    tasks = await db.fetch_all("SELECT * FROM tasks WHERE plan_id = ?", (plan["id"],))
    assert len(tasks) == 1


@pytest.mark.integration
async def test_promote_missing_doc_returns_404(
    db: Database,
    client: AsyncClient,
    auth_headers: dict[str, str],
    mocker: Any,
) -> None:
    import uuid

    await seed_user(db)
    pid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, "test-user", "p", "https://example.com/r.git", "qwen"),
    )
    mocker.patch.object(
        client.app.state.brainstorm,
        "read_doc",
        new=mocker.AsyncMock(side_effect=FileNotFoundError("nope")),
    )
    resp = await client.post(
        "/api/plans/promote",
        headers=auth_headers,
        json={"project_id": pid, "plan_path": "docs/superpowers/plans/missing.md"},
    )
    assert resp.status_code == 404


async def _seed_plan_with_passed_tasks(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    n: int = 2,
) -> tuple[str, list[str]]:
    """Seed one plan with N tasks in PASSED state, each with a pr_url."""
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], "Build things")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Things",
            "plan_slug": "things",
            "tasks": [
                {
                    "title": f"Task {i}",
                    "slug": f"task-{i}",
                    "description": f"Do thing {i}",
                    "depends_on": [],
                }
                for i in range(n)
            ],
        },
        "plan/2026-06-01-things",
    )
    tasks = await queue.get_tasks_for_plan(plan_id)
    passed_ids = []
    for task in tasks:
        await queue.update_task_status(task["id"], TaskStatus.PASSED)
        await queue.set_task_pr_url(
            task["id"], f"https://github.com/u/a/pull/{task['id']}"
        )
        passed_ids.append(task["id"])
    return plan_id, passed_ids


@pytest.mark.integration
async def test_approve_merges_batch(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved: list[str] = []

    async def fake_approve(task_id: str, project: dict) -> None:
        approved.append(task_id)

    plan_id, passed_ids = await _seed_plan_with_passed_tasks(
        client, db, auth_headers, n=2
    )
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_task_merge", fake_approve
    )
    resp = await client.post(
        f"/api/plans/{plan_id}/approve-merges", headers=auth_headers
    )
    assert resp.status_code == 200
    assert sorted(approved) == sorted(passed_ids)
    assert resp.json()["approved"] == 2


@pytest.mark.integration
async def test_approve_merges_integrates_a_finished_plan(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last link: once every task is merged, the plan merges to its base.

    Run #5's blocker in one test. Before this, `approve-merges` against a
    finished plan returned ``approved: 0`` and stopped, leaving the work on
    the plan branch and the integration PR unmentioned anywhere a user looks.
    """
    integrated: list[str] = []

    async def fake_integrate(plan_id: str, project: dict) -> str:
        integrated.append(plan_id)
        return "https://github.com/u/a/pull/48"

    plan_id, task_ids = await _seed_plan_with_passed_tasks(
        client, db, auth_headers, n=2
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    for task_id in task_ids:
        await queue.mark_merged(task_id)
    await queue.set_plan_integration_pr(plan_id, "https://github.com/u/a/pull/48")
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_plan_integration", fake_integrate
    )

    resp = await client.post(
        f"/api/plans/{plan_id}/approve-merges", headers=auth_headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["approved"] == 0
    assert body["integration"]["status"] == "merged"
    assert body["integration"]["pr_url"] == "https://github.com/u/a/pull/48"
    assert integrated == [plan_id]


@pytest.mark.integration
async def test_approve_merges_refuses_to_integrate_an_unfinished_plan(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete the remaining-task check and a partial plan lands on the base branch.

    The tasks here stay PASSED (parked, never merged) because the approve
    stub does not merge them, so the plan branch does not carry their work.
    """
    integrated: list[str] = []

    async def fake_approve(task_id: str, project: dict) -> None:
        return None

    async def fake_integrate(plan_id: str, project: dict) -> str:
        integrated.append(plan_id)
        return "https://github.com/u/a/pull/48"

    plan_id, _ = await _seed_plan_with_passed_tasks(client, db, auth_headers, n=2)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.set_plan_integration_pr(plan_id, "https://github.com/u/a/pull/48")
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_task_merge", fake_approve
    )
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_plan_integration", fake_integrate
    )

    resp = await client.post(
        f"/api/plans/{plan_id}/approve-merges", headers=auth_headers
    )

    assert resp.status_code == 200
    assert resp.json()["integration"]["status"] == "not_ready"
    assert integrated == []


@pytest.mark.integration
async def test_approve_merges_does_not_integrate_when_a_task_merge_failed(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed task merge must suppress integration, not be papered over by it."""
    integrated: list[str] = []

    async def fake_approve_raises(task_id: str, project: dict) -> None:
        msg = "merge exploded"
        raise RuntimeError(msg)

    async def fake_integrate(plan_id: str, project: dict) -> str:
        integrated.append(plan_id)
        return "https://github.com/u/a/pull/48"

    plan_id, _ = await _seed_plan_with_passed_tasks(client, db, auth_headers, n=1)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.set_plan_integration_pr(plan_id, "https://github.com/u/a/pull/48")
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_task_merge", fake_approve_raises
    )
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_plan_integration", fake_integrate
    )

    resp = await client.post(
        f"/api/plans/{plan_id}/approve-merges", headers=auth_headers
    )

    assert resp.status_code == 200
    assert resp.json()["integration"]["status"] == "not_ready"
    assert integrated == []


@pytest.mark.integration
async def test_pending_approvals_lists_a_plan_awaiting_integration(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """`GET /api/approvals/pending` must name the integration PR.

    The CLI, the dashboard and the loop digest all read this one endpoint, so
    a plan missing here is a plan missing from every surface a user polls.
    """
    plan_id, task_ids = await _seed_plan_with_passed_tasks(
        client, db, auth_headers, n=1
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    for task_id in task_ids:
        await queue.mark_merged(task_id)
    await queue.update_plan_status(plan_id, "completed")
    await queue.set_plan_integration_pr(plan_id, "https://github.com/u/a/pull/48")

    resp = await client.get("/api/approvals/pending", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["plan_count"] == 1
    assert body["plans"][0]["plan_id"] == plan_id
    assert body["plans"][0]["pr_url"] == "https://github.com/u/a/pull/48"

    # ...and drops out again the moment it lands.
    await queue.mark_plan_integrated(plan_id)
    after = await client.get("/api/approvals/pending", headers=auth_headers)
    assert after.json()["count"] == 0


@pytest.mark.integration
async def test_get_plan_returns_error_reason(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)
    project_id = await _create_project(client, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project_id, "Build things")
    await queue.set_plan_error(plan_id, "review response not valid JSON")

    resp = await client.get(f"/api/plans/{plan_id}", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["error"] == "review response not valid JSON"


@pytest.mark.integration
async def test_approve_merges_returns_generic_error_message(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approve-merges must return a generic error message and log the exception details server-side."""
    secret_token = "ghp_AAAAAAAAAAAAAAAAAAA1234567890"

    async def fake_approve_raises(task_id: str, project: dict) -> None:
        msg = f"Git command failed (exit 1): gh pr merge\ntoken={secret_token}"
        raise RuntimeError(msg)

    plan_id, _ = await _seed_plan_with_passed_tasks(client, db, auth_headers, n=1)
    monkeypatch.setattr(
        client.app.state.orchestrator, "approve_task_merge", fake_approve_raises
    )
    resp = await client.post(
        f"/api/plans/{plan_id}/approve-merges", headers=auth_headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["approved"] == 0
    assert len(body["errors"]) == 1
    err_msg = body["errors"][0]["error"]
    assert err_msg == "Failed to approve task merge due to an internal error"
    assert secret_token not in err_msg


@pytest.mark.integration
async def test_pending_approvals_lists_an_autonomous_proposal(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The endpoint's own query must not filter proposals out.

    `summarize_pending` can classify a proposal perfectly and still surface
    nothing, because the WHERE clause here decides which rows it ever sees.
    That is the seam where this fix would be unit-green and inert: the
    predicate has tests, the CLI has tests, and `praxis pending` would still
    print "Nothing awaiting approval" over a real proposal.
    """
    await seed_user(db)
    project = await client.post(
        "/api/projects",
        json={"name": "App", "repo_url": "https://github.com/u/a", "model_name": "m"},
        headers=auth_headers,
    )
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project.json()["id"], source="autonomous")

    resp = await client.get("/api/approvals/pending", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["proposal_count"] == 1
    assert body["proposals"][0]["plan_id"] == plan_id
    # Separate gate: it is not counted among the merge-gate items.
    assert body["count"] == 0

    # ...and drops out the moment the human answers it.
    await queue.update_plan_status(plan_id, "rejected")
    after = await client.get("/api/approvals/pending", headers=auth_headers)
    assert after.json()["proposal_count"] == 0
