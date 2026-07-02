"""Security hardening tests: callback auth, timing-safe token compare, repo_url."""
# ruff: noqa: S101, S105

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import AsyncClient
from pydantic import ValidationError

from orchestrator.api.auth import verify_token
from orchestrator.config import Settings
from orchestrator.database import Database
from orchestrator.models.schemas import ProjectCreate


# ---------------------------------------------------------------------------
# FIX 1 — Callback token authentication
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_callback_valid_token_passes(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A callback with the correct X-Praxis-Callback-Token header returns 200."""
    from orchestrator.models.schemas import TaskStatus
    from tests.test_api_tasks import _setup_plan_with_task

    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await queue.create_agent_run(task_id, "container-sec-test")

    # Set the secret on app.state so the check is active.
    client.app.state.internal_callback_secret = "my-secret"  # type: ignore[attr-defined]
    try:
        response = await client.post(
            "/api/internal/agent-done",
            json={
                "task_id": task_id,
                "run_id": run_id,
                "status": "completed",
            },
            headers={"X-Praxis-Callback-Token": "my-secret"},
        )
        assert response.status_code == 200
    finally:
        # Reset so other tests are unaffected.
        del client.app.state.internal_callback_secret  # type: ignore[attr-defined]


@pytest.mark.integration
async def test_callback_missing_token_returns_401(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A callback without the secret header is rejected with 401."""
    client.app.state.internal_callback_secret = "my-secret"  # type: ignore[attr-defined]
    try:
        response = await client.post(
            "/api/internal/agent-done",
            json={"task_id": "any", "status": "completed"},
            # no X-Praxis-Callback-Token header
        )
        assert response.status_code == 401
    finally:
        del client.app.state.internal_callback_secret  # type: ignore[attr-defined]


@pytest.mark.integration
async def test_callback_wrong_token_returns_401(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """A callback with a wrong secret is rejected with 401."""
    client.app.state.internal_callback_secret = "correct-secret"  # type: ignore[attr-defined]
    try:
        response = await client.post(
            "/api/internal/agent-done",
            json={"task_id": "any", "status": "completed"},
            headers={"X-Praxis-Callback-Token": "wrong-secret"},
        )
        assert response.status_code == 401
    finally:
        del client.app.state.internal_callback_secret  # type: ignore[attr-defined]


@pytest.mark.integration
async def test_callback_no_secret_configured_fails_closed(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """When internal_callback_secret is unset, the callback is rejected (503)."""
    from tests.test_api_tasks import _setup_plan_with_task

    # Remove the secret the fixture set, to simulate a misconfigured deploy.
    if hasattr(client.app.state, "internal_callback_secret"):
        del client.app.state.internal_callback_secret  # type: ignore[attr-defined]

    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    run_id = await queue.create_agent_run(task_id, "container-noauth")

    response = await client.post(
        "/api/internal/agent-done",
        json={"task_id": task_id, "run_id": run_id, "status": "completed"},
        headers={"X-Praxis-Callback-Token": "anything"},
    )
    assert response.status_code == 503
    # The fixture sets the secret; restore it so later tests are unaffected.
    client.app.state.internal_callback_secret = "test-auth"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# FIX 2 — Timing-safe auth token comparison
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_verify_token_uses_compare_digest_valid() -> None:
    """verify_token accepts a correct token."""
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="good")
    settings = Settings(auth_token="good", github_token="gh")
    token = await verify_token(credentials=credentials, settings=settings)
    assert token == "good"


@pytest.mark.unit
async def test_verify_token_invalid_still_raises_401() -> None:
    """verify_token rejects a wrong token with 401 (compare_digest path)."""
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad")
    settings = Settings(auth_token="good", github_token="gh")
    with pytest.raises(HTTPException) as exc_info:
        await verify_token(credentials=credentials, settings=settings)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# FIX 3 — repo_url validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_repo_url_valid_https() -> None:
    """Standard GitHub HTTPS URL is accepted."""
    p = ProjectCreate(
        name="test",
        repo_url="https://github.com/user/repo",
        model_name="m",
    )
    assert p.repo_url == "https://github.com/user/repo"


@pytest.mark.unit
def test_repo_url_valid_scp_ssh() -> None:
    """Standard scp-style SSH URL is accepted."""
    p = ProjectCreate(
        name="test",
        repo_url="git@github.com:user/repo.git",
        model_name="m",
    )
    assert p.repo_url == "git@github.com:user/repo.git"


@pytest.mark.unit
def test_repo_url_rejects_ext_injection() -> None:
    """ext:: git transport used for RCE is rejected."""
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="test",
            repo_url="ext::sh -c id",
            model_name="m",
        )


@pytest.mark.unit
def test_repo_url_rejects_file_scheme() -> None:
    """file:// URL is rejected."""
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="test",
            repo_url="file:///etc/passwd",
            model_name="m",
        )


@pytest.mark.unit
def test_repo_url_rejects_upload_pack_option() -> None:
    """repo_url containing --upload-pack (git option injection) is rejected."""
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="test",
            repo_url="https://github.com/u/r --upload-pack=/bin/sh",
            model_name="m",
        )


@pytest.mark.unit
def test_repo_url_rejects_empty() -> None:
    """Empty repo_url is rejected."""
    with pytest.raises(ValidationError):
        ProjectCreate(name="test", repo_url="", model_name="m")


@pytest.mark.unit
def test_repo_url_rejects_git_protocol() -> None:
    """git:// scheme is rejected (unauthenticated, can be MITM'd)."""
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="test",
            repo_url="git://github.com/user/repo.git",
            model_name="m",
        )


@pytest.mark.unit
def test_repo_url_rejects_ssh_scheme() -> None:
    """ssh:// scheme is rejected (only scp-style git@ is allowed)."""
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="test",
            repo_url="ssh://git@github.com/user/repo.git",
            model_name="m",
        )
