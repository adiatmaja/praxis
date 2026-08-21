"""A status code is an assertion about WHOSE fault it is.

500 means "the server is broken, this is my fault, retry or file a bug". So a
KNOWN, anticipated condition answering 500 is a false report: the operator goes
looking for a server bug when the real cause was a missing credential or a
typo in their own settings file, either of which they could fix in ten seconds
if anyone had told them.

The other half of the class is a 200 whose body claims work that did not
happen. `POST /tasks/{id}/stop` is the case here: it reported containers
stopped on a host that had no Docker to stop them with.

Grouped by CONDITION rather than by route, because the defect these keep
producing is a fix landing on some of a family and not the rest.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest_asyncio.fixture
async def project_id(db: Database, client: AsyncClient) -> str:
    user_id = await seed_user(db)
    pid = "proj-err"
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate,
            confidence_threshold, max_retries, max_improvement_cycles,
            model_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pid, user_id, "app", "https://github.com/u/r", "main", True, 0.8, 3, 3, "m"),
    )
    return pid


@pytest.mark.integration
async def test_a_malformed_settings_file_names_itself_rather_than_500ing(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings file is MOUNTED and hand-edited, so a syntax error in it is
    an ordinary operator mistake.

    `load_yaml_settings` raises `ValueError("Invalid YAML in <path>: <where>")`,
    naming the file and the parse position. Four routes threw that away and
    answered a bare 500, so the one message the operator needed stayed in the
    container log while the dashboard told them the server was broken.
    """
    es = client.app.state.effective_settings  # type: ignore[attr-defined]

    async def boom() -> Any:
        message = "Invalid YAML in /app/config/example.yaml: line 4, column 3"
        raise ValueError(message)

    monkeypatch.setattr(es, "role_chains", boom)
    resp = await client.get("/api/settings/roles", headers=auth_headers)

    assert resp.status_code == 503
    assert "Invalid YAML" in resp.json()["detail"]
    assert "line 4" in resp.json()["detail"]


@pytest.mark.integration
async def test_a_shadowed_model_override_is_not_reported_as_applied(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored override that a role chain replaces has NOT taken effect.

    `EffectiveSettings.call_site_chain` returns the role's chain whenever the
    call site has a role and that role declares one, and never consults this
    override. On a stock install that covers most call sites, so a bare
    `{"status": "ok"}` told the operator their change had landed while the loop
    went on running the role chain.
    """
    es = client.app.state.effective_settings  # type: ignore[attr-defined]

    async def chains() -> dict[str, list[str]]:
        return {"plan": ["sonnet", "opus"]}

    monkeypatch.setattr(es, "role_chains", chains)
    resp = await client.put(
        "/api/settings/models",
        headers=auth_headers,
        json={"call_site": "plan_spec", "config": {"provider": "codex"}},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stored_but_shadowed"
    assert body["shadowed_by_role"] == "plan"
    assert "will NOT take effect" in body["detail"]


@pytest.mark.integration
async def test_an_unshadowed_model_override_still_reports_ok(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other branch, so the warning cannot become unconditional noise."""
    es = client.app.state.effective_settings  # type: ignore[attr-defined]

    async def chains() -> dict[str, list[str]]:
        return {}

    monkeypatch.setattr(es, "role_chains", chains)
    resp = await client.put(
        "/api/settings/models",
        headers=auth_headers,
        json={"call_site": "plan_spec", "config": {"provider": "codex"}},
    )
    assert resp.json()["status"] == "ok"


@pytest.mark.integration
async def test_stop_reports_what_it_actually_stopped(
    client: AsyncClient, db: Database, auth_headers: dict[str, str], project_id: str
) -> None:
    """`stopped` counts run ROWS closed, not containers killed.

    `main.py` deliberately tolerates a host with no Docker and logs "Agent
    manager unavailable". On such a host this route still closed every run row
    and answered `{"stopped": 1}` having contacted nothing, which reads as a
    container killed while it keeps running.
    """
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    plan_id = await queue.create_plan(project_id, "p")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "s",
            "plan_slug": "s",
            "tasks": [
                {"title": "t", "slug": "t", "description": "d", "depends_on": []}
            ],
        },
        "plan/x",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    await queue.create_agent_run(task_id, "container-1")
    client.app.state.agent_manager = None  # type: ignore[attr-defined]

    resp = await client.post(f"/api/tasks/{task_id}/stop", headers=auth_headers)

    body = resp.json()
    assert body["stopped"] == 1, "the run row was closed"
    assert body["containers_stopped"] == 0, "but nothing was actually stopped"
    assert body["docker_available"] is False


@pytest.mark.integration
async def test_a_pre_seeding_database_names_the_real_remedy(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """ "Seed a user first" named an action no verb performs.

    The default user is auto-seeded at startup, so this fires only on a
    database that predates that, whose actual remedy is to delete the file and
    restart. It is a recoverable state of the install, so 503 rather than 500.
    """
    await db.execute("DELETE FROM users")
    resp = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "x",
            "repo_url": "https://github.com/u/x",
            "model_name": "m",
        },
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "delete data/orchestrator.db" in detail
    assert "Seed a user first" not in detail


@pytest.mark.integration
async def test_a_repo_read_failure_carries_the_git_reason_on_every_route(
    client: AsyncClient, auth_headers: dict[str, str], project_id: str, mocker
) -> None:
    """The lifecycle routes were in the family and bypassed the shared guard.

    They answered the right code (502) with the wrong body: `str()` on a
    `CalledProcessError` is only the exit status, and the reason an operator can
    act on sits unread on `.stderr`. Route them through `guard_repo_access` and
    the reason survives.
    """
    import subprocess

    error = subprocess.CalledProcessError(
        128, "git clone", stderr=b"fatal: could not read Username"
    )
    mocker.patch.object(
        client.app.state.brainstorm,
        "list_lifecycle_docs",
        new=mocker.AsyncMock(side_effect=error),
    )
    resp = await client.get(
        f"/api/projects/{project_id}/lifecycle", headers=auth_headers
    )
    assert resp.status_code == 502
    assert "could not read Username" in resp.json()["detail"]


@pytest.mark.integration
async def test_a_missing_doc_is_still_404_through_the_shared_guard(
    client: AsyncClient, auth_headers: dict[str, str], project_id: str, mocker
) -> None:
    """The guard must not turn "you asked for a file that is not there" into a
    502 about the remote. Both routes it replaced had this branch."""
    mocker.patch.object(
        client.app.state.brainstorm,
        "read_doc",
        new=mocker.AsyncMock(side_effect=FileNotFoundError("docs/nope.md")),
    )
    resp = await client.get(
        f"/api/projects/{project_id}/doc-raw?path=docs/nope.md", headers=auth_headers
    )
    assert resp.status_code == 404
    assert "nope.md" in resp.json()["detail"]
