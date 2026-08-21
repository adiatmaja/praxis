"""A missing GitHub credential is configuration, and must not read as a crash.

Found on the live install in walkthrough #10, on the very next command after
`praxis init`: `praxis add-project playground https://github.com/...` printed
`Error 500: Internal Server Error`. The server knew exactly what was wrong,
because `build_credential_provider` raises `CredentialError` carrying the
remedy verbatim ("set GITHUB_TOKEN, or GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY"),
and that sentence stayed in the container log while the operator got nine words
saying the SERVER is broken.

It is the class in its purest form: the system holds the true answer and
reports something else. It is also the worst possible place for it, because
the doctor had just said, correctly, "local mode: no GitHub credential
configured, which is correct for evaluating with a file:// repo". Both
statements are true, nothing connects them, and a 500 tells you to go looking
at the server.

Two endpoints built the provider without catching it. They need different
answers, and each is asserted here:

- `POST /api/projects` cannot proceed. A GitHub URL with no credential is a
  request that cannot be served, so it is a 422 that names the remedy.
- `GET /projects/{id}/git-state` is a visualization poll that already reports
  "unavailable, and here is why" for a RuntimeError. A missing credential is
  one more reason, so it degrades rather than failing, and it must not 500 on
  every poll of the shipped local-evaluation mode.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


@pytest.fixture
def _no_github_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the credential builder to raise, as an unconfigured install does."""
    from orchestrator.core.github_credentials import CredentialError

    def _raise(_settings: Any) -> Any:
        message = (
            "no GitHub credentials configured: set GITHUB_TOKEN, or "
            "GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY"
        )
        raise CredentialError(message)

    monkeypatch.setattr("orchestrator.api.projects.build_credential_provider", _raise)
    monkeypatch.setattr("orchestrator.api.git_state.build_credential_provider", _raise)


@pytest.mark.integration
@pytest.mark.usefixtures("_no_github_credential")
async def test_creating_a_project_without_a_credential_is_not_a_500(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """422 with the remedy, not 500 with nothing."""
    await seed_user(db)
    response = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "playground",
            "repo_url": "https://github.com/adiatmaja/playground",
            "model_name": "qwen3.8-27b",
        },
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "GITHUB_TOKEN" in detail, "the remedy the exception already carried"
    assert "file://" in detail, "and the credential-free alternative"


@pytest.mark.integration
@pytest.mark.usefixtures("_no_github_credential")
async def test_git_state_degrades_instead_of_failing_the_poll(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The dashboard polls this. Local mode must not 500 on every tick.

    `available: false` with a reason is the shape this endpoint already
    returns when the remote cannot be read, so a missing credential belongs
    there rather than in a traceback.
    """
    await seed_user(db)
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, default_branch, "
        "model_name) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "proj-1",
            "test-user",
            "playground",
            "https://github.com/adiatmaja/playground",
            "main",
            "qwen3.8-27b",
        ),
    )

    response = await client.get("/api/projects/proj-1/git-state", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False
    assert "GITHUB_TOKEN" in body["detail"]
