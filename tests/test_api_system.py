"""System API tests."""
# ruff: noqa: S101

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.database import Database
from tests.conftest import seed_user


# ---------------------------------------------------------------------------
# Helpers for provider-probe mocking
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_which(mocker: pytest.MonkeyPatch) -> None:
    """Resolve provider shims to their own name so probe tests don't depend on
    the real CLIs being installed (the probe now calls shutil.which)."""
    mocker.patch("orchestrator.api.system.shutil.which", side_effect=lambda name: name)


def _make_proc(mocker: pytest.MonkeyPatch, returncode: int) -> object:
    """Return an async-compatible fake process with the given return code."""
    proc = mocker.MagicMock()
    proc.returncode = returncode
    proc.wait = mocker.AsyncMock(return_value=returncode)
    return proc


@pytest.mark.integration
async def test_status_and_opus_state(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    status = await client.get("/api/status", headers=auth_headers)
    opus = await client.get("/api/opus/state", headers=auth_headers)

    assert status.status_code == 200
    assert "opus_state" in status.json()
    assert "active_agents" in status.json()
    assert opus.status_code == 200
    assert opus.json()["status"] == "available"
    assert opus.json()["queued_count"] == 0


@pytest.mark.integration
async def test_status_includes_agent_model(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/status", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert "agent_model" in data
    assert data["agent_model"]["name"] == "claude-opus-4-8"
    assert isinstance(data["agent_model"]["connected"], bool)


@pytest.mark.integration
async def test_status_includes_subagent_model(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)

    response = await client.get("/api/status", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert "subagent_model" in data
    assert "name" in data["subagent_model"]
    assert isinstance(data["subagent_model"]["connected"], bool)


# ---------------------------------------------------------------------------
# Provider probe tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_probe_provider_cli_missing(mocker: pytest.MonkeyPatch) -> None:
    """OSError (binary not found) → cli_available=False, authenticated=False."""
    import orchestrator.api.system as sys_mod

    # Clear cache so prior test state doesn't bleed in
    sys_mod._provider_probe_cache.clear()

    mocker.patch(
        "asyncio.create_subprocess_exec",
        new=mocker.AsyncMock(side_effect=OSError("not found")),
    )
    result = await sys_mod._probe_provider("claude")
    assert result["cli_available"] is False
    assert result["authenticated"] is False
    assert result["name"] == "claude"
    assert "login_hint" in result


@pytest.mark.unit
async def test_probe_provider_codex_auth_ok(mocker: pytest.MonkeyPatch) -> None:
    """codex --version exit 0 + codex login status exit 0 → authenticated=True."""
    import orchestrator.api.system as sys_mod

    sys_mod._provider_probe_cache.clear()

    call_count = 0

    async def _fake_exec(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        proc = mocker.MagicMock()
        proc.returncode = 0
        proc.wait = mocker.AsyncMock(return_value=0)
        return proc

    mocker.patch("asyncio.create_subprocess_exec", new=_fake_exec)
    result = await sys_mod._probe_provider("codex")
    assert result["cli_available"] is True
    assert result["authenticated"] is True
    assert call_count == 2  # version probe + auth probe


@pytest.mark.unit
async def test_probe_provider_codex_auth_fail(mocker: pytest.MonkeyPatch) -> None:
    """codex --version exit 0 but codex login status nonzero → authenticated=False."""
    import orchestrator.api.system as sys_mod

    sys_mod._provider_probe_cache.clear()

    call_index = 0

    async def _fake_exec(*args: object, **kwargs: object) -> object:
        nonlocal call_index
        rc = 0 if call_index == 0 else 1
        call_index += 1
        proc = mocker.MagicMock()
        proc.returncode = rc
        proc.wait = mocker.AsyncMock(return_value=rc)
        return proc

    mocker.patch("asyncio.create_subprocess_exec", new=_fake_exec)
    result = await sys_mod._probe_provider("codex")
    assert result["cli_available"] is True
    assert result["authenticated"] is False


@pytest.mark.unit
async def test_probe_provider_timeout_handled(mocker: pytest.MonkeyPatch) -> None:
    """TimeoutError from asyncio.wait_for is caught; returns cli_available=False."""
    import orchestrator.api.system as sys_mod

    sys_mod._provider_probe_cache.clear()

    async def _fake_exec(*args: object, **kwargs: object) -> object:
        proc = mocker.MagicMock()
        proc.returncode = None
        proc.wait = mocker.AsyncMock(side_effect=TimeoutError)
        return proc

    mocker.patch("asyncio.create_subprocess_exec", new=_fake_exec)
    result = await sys_mod._probe_provider("agy")
    assert result["cli_available"] is False
    assert result["authenticated"] is False


@pytest.mark.unit
async def test_probe_provider_agy_no_auth_cmd(mocker: pytest.MonkeyPatch) -> None:
    """agy has no auth command; cli_available=True → authenticated=True (best-effort)."""
    import orchestrator.api.system as sys_mod

    sys_mod._provider_probe_cache.clear()

    async def _fake_exec(*args: object, **kwargs: object) -> object:
        proc = mocker.MagicMock()
        proc.returncode = 0
        proc.wait = mocker.AsyncMock(return_value=0)
        return proc

    mocker.patch("asyncio.create_subprocess_exec", new=_fake_exec)
    result = await sys_mod._probe_provider("agy")
    assert result["cli_available"] is True
    assert result["authenticated"] is True


@pytest.mark.integration
async def test_status_includes_providers(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mocker: pytest.MonkeyPatch,
) -> None:
    """GET /api/status response contains a 'providers' list with the right shape."""
    import orchestrator.api.system as sys_mod

    sys_mod._provider_probe_cache.clear()
    await seed_user(db)

    # Stub all subprocess calls so the test is hermetic
    mocker.patch(
        "asyncio.create_subprocess_exec",
        new=mocker.AsyncMock(side_effect=OSError("not found")),
    )

    response = await client.get("/api/status", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "providers" in data
    providers = data["providers"]
    assert isinstance(providers, list)
    assert len(providers) == 3
    names = [p["name"] for p in providers]
    assert names == ["claude", "codex", "agy"]
    for p in providers:
        assert isinstance(p["cli_available"], bool)
        assert isinstance(p["authenticated"], bool)
        assert isinstance(p["login_hint"], str)
