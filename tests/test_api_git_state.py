from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import seed_user


@pytest.fixture(autouse=True)
def _mock_preflight(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


async def _make_project(client, auth_headers, db=None):
    if db is not None:
        await seed_user(db)
    r = await client.post(
        "/api/projects",
        json={"name": "gs", "repo_url": "https://github.com/o/r", "model_name": "m"},
        headers=auth_headers,
    )
    return r.json()["id"]


async def test_git_state_returns_origin_head(client, auth_headers, db):
    project_id = await _make_project(client, auth_headers, db)
    with patch("orchestrator.api.git_state.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(return_value="abc1234def")
        inst.remote_commit_meta = AsyncMock(
            return_value={
                "subject": "fix things",
                "committed_at": "2026-07-06T05:19:58Z",
            }
        )
        mock_git.repo_slug.return_value = "o/r"
        resp = await client.get(
            f"/api/projects/{project_id}/git-state", headers=auth_headers
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sha"] == "abc1234def"
    assert body["short_sha"] == "abc1234"
    assert body["subject"] == "fix things"
    assert body["available"] is True


async def test_git_state_unavailable_on_remote_error(client, auth_headers, db):
    project_id = await _make_project(client, auth_headers, db)
    with patch("orchestrator.api.git_state.GitOps") as mock_git:
        inst = mock_git.return_value
        inst.remote_head_sha = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await client.get(
            f"/api/projects/{project_id}/git-state", headers=auth_headers
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["detail"]


async def test_git_state_404_for_unknown_project(client, auth_headers):
    resp = await client.get(
        "/api/projects/does-not-exist/git-state", headers=auth_headers
    )
    assert resp.status_code == 404
