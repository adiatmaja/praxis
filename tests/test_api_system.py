"""System API tests."""
# ruff: noqa: S101

from __future__ import annotations

import httpx
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


# ---------------------------------------------------------------------------
# Build stamp tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_health_includes_build(client: AsyncClient) -> None:
    """GET /health includes a 'build' dict with commit and started_at."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "build" in body
    assert body["build"]["commit"]
    assert body["build"]["started_at"]


@pytest.mark.integration
async def test_status_includes_build(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mocker: pytest.MonkeyPatch,
) -> None:
    """GET /api/status includes a 'build' key."""
    import orchestrator.api.system as sys_mod

    sys_mod._provider_probe_cache.clear()
    await seed_user(db)

    # Stub subprocess calls so probe doesn't deadlock on missing CLIs.
    mocker.patch(
        "asyncio.create_subprocess_exec",
        new=mocker.AsyncMock(side_effect=OSError("not found")),
    )

    response = await client.get("/api/status", headers=auth_headers)
    assert response.status_code == 200
    assert "build" in response.json()


# ---------------------------------------------------------------------------
# Bench mode: the orchestrator's ACTUAL arm, reported so the bench can check it
# ---------------------------------------------------------------------------
#
# core/bench_mode.py reads PRAXIS_BENCH and PRAXIS_BENCH_DISABLE_VERIFY with
# os.environ INSIDE this process.  bench/runner.py is a SEPARATE process talking
# REST, so nothing it sets reaches here.  Without these two fields an A,C
# invocation aimed at an orchestrator started without the flags produces rows
# carrying the right condition label and the wrong arm, and no number anywhere
# says so.  Everything below therefore pins that the reported booleans track the
# real environment rather than a constant or a cached first reading.


def _stub_probes(mocker: pytest.MonkeyPatch) -> None:
    """Make the CLI probes hermetic so a status call cannot touch the machine."""
    import orchestrator.api.system as sys_mod

    sys_mod._provider_probe_cache.clear()
    mocker.patch(
        "asyncio.create_subprocess_exec",
        new=mocker.AsyncMock(side_effect=OSError("not found")),
    )


def _clear_bench_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("bench_env", "expected_bench_mode", "expected_gate_disabled"),
    [
        ({}, False, False),
        ({"PRAXIS_BENCH": "1"}, True, False),
        ({"PRAXIS_BENCH_DISABLE_VERIFY": "1"}, False, False),
        ({"PRAXIS_BENCH": "1", "PRAXIS_BENCH_DISABLE_VERIFY": "1"}, True, True),
        ({"PRAXIS_BENCH": "true", "PRAXIS_BENCH_DISABLE_VERIFY": "true"}, False, False),
        ({"PRAXIS_BENCH": "0", "PRAXIS_BENCH_DISABLE_VERIFY": "0"}, False, False),
    ],
    ids=[
        "neither-flag",
        "bench-flag-only",
        "disable-flag-only",
        "both-flags",
        "truthy-but-not-the-literal-1",
        "explicitly-zero",
    ],
)
async def test_status_reports_the_orchestrators_real_bench_mode(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mocker: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    bench_env: dict[str, str],
    expected_bench_mode: bool,
    expected_gate_disabled: bool,
) -> None:
    """Both booleans must equal what core/bench_mode.py says, for every combo.

    The ``neither-flag`` case is the one that matters most: it is the shape of a
    normal deployment, and it is exactly what an endpoint hardcoded to report a
    bench mode would get wrong.  ``truthy-but-not-the-literal-1`` pins that the
    endpoint inherits the double-gated literal check rather than reimplementing
    a loose truthiness test on its own.
    """
    _stub_probes(mocker)
    await seed_user(db)
    _clear_bench_env(monkeypatch)
    for key, value in bench_env.items():
        monkeypatch.setenv(key, value)

    response = await client.get("/api/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["bench_mode"] is expected_bench_mode
    assert body["verify_gate_disabled"] is expected_gate_disabled


@pytest.mark.integration
async def test_status_reads_bench_mode_live_instead_of_caching_it(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mocker: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call must see a changed environment, not the first reading.

    ``system.py`` caches the claude CLI probe for 60 seconds because it spawns a
    subprocess.  Copying that pattern here would be actively harmful: the whole
    point of the field is to tell an operator whether the RESTART they just did
    took effect, and a cached value would answer with the mode from before it.
    """
    _stub_probes(mocker)
    await seed_user(db)
    _clear_bench_env(monkeypatch)

    before = await client.get("/api/status", headers=auth_headers)

    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")
    after = await client.get("/api/status", headers=auth_headers)

    assert before.json()["bench_mode"] is False
    assert before.json()["verify_gate_disabled"] is False
    assert after.json()["bench_mode"] is True
    assert after.json()["verify_gate_disabled"] is True


@pytest.mark.integration
async def test_status_bench_fields_delegate_to_core_bench_mode(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mocker: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint must call the two functions, not re-derive the rule.

    The environment is CLEARED here, so ``(True, True)`` cannot come from it:
    both fields have to be the patched functions' return values.  And a handler
    that inlined ``os.environ.get("PRAXIS_BENCH") == "1"`` instead of importing
    would have no such attributes to patch, so ``mocker.patch`` raises and this
    still fails.  The rule stays in one place either way.
    """
    _stub_probes(mocker)
    await seed_user(db)
    _clear_bench_env(monkeypatch)
    mocker.patch("orchestrator.api.system.bench_mode", return_value=True)
    mocker.patch("orchestrator.api.system.verify_gate_disabled", return_value=True)

    response = await client.get("/api/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["bench_mode"] is True
    assert response.json()["verify_gate_disabled"] is True


# ---------------------------------------------------------------------------
# /api/lm-models: embedding-model identification (Task 13)
#
# The OpenAI-compatible /v1/models endpoint (the primary connectivity+list
# check) carries no field distinguishing an embedding model from an
# implementer model. LM Studio's own /api/v0/models does, via "type". These
# tests pin that the endpoint enriches from /api/v0/models on a best-effort
# basis rather than guessing from the model id string, and that a failure of
# that SECOND call never disturbs the primary connectivity result.
# ---------------------------------------------------------------------------


def _lm_response(url: str, data: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=data, request=httpx.Request("GET", url))


# The FastAPI TestClient's own ASGI transport is ALSO an httpx.AsyncClient
# under the hood (see conftest.py's `client` fixture), so patching
# httpx.AsyncClient.get globally intercepts the test's own
# `client.get("/api/lm-models", ...)` call too, not just the endpoint's
# outbound calls to the fake LM Studio URL. Every fake `_fake_get` below
# falls through to the real implementation for anything it doesn't
# recognize, so the test client's own traffic is unaffected.
_real_async_client_get = httpx.AsyncClient.get


@pytest.mark.integration
async def test_lm_models_identifies_embedding_models_via_v0_endpoint(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mocker: pytest.MonkeyPatch,
) -> None:
    """embedding_model_ids reflects the real "type" field from /api/v0/models."""
    await seed_user(db)

    async def _fake_get(
        self: httpx.AsyncClient, url: str, *a: object, **kw: object
    ) -> httpx.Response:
        url = str(url)
        if url.endswith("/v1/models"):
            return _lm_response(
                url,
                {
                    "data": [
                        {"id": "qwen3.6-27b"},
                        {"id": "text-embedding-nomic-embed-text-v1.5"},
                    ]
                },
            )
        if url.endswith("/api/v0/models"):
            return _lm_response(
                url,
                {
                    "data": [
                        {"id": "qwen3.6-27b", "type": "llm"},
                        {
                            "id": "text-embedding-nomic-embed-text-v1.5",
                            "type": "embeddings",
                        },
                    ]
                },
            )
        return await _real_async_client_get(self, url, *a, **kw)

    mocker.patch("httpx.AsyncClient.get", new=_fake_get)

    response = await client.get("/api/lm-models", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["models"] == ["qwen3.6-27b", "text-embedding-nomic-embed-text-v1.5"]
    assert body["embedding_model_ids"] == ["text-embedding-nomic-embed-text-v1.5"]


@pytest.mark.integration
async def test_lm_models_embedding_ids_empty_when_v0_endpoint_unavailable(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
    mocker: pytest.MonkeyPatch,
) -> None:
    """A v0-enrichment failure must not disturb the primary /v1/models result
    and must degrade to an empty list, never a name-substring guess."""
    await seed_user(db)

    async def _fake_get(
        self: httpx.AsyncClient, url: str, *a: object, **kw: object
    ) -> httpx.Response:
        url = str(url)
        if url.endswith("/v1/models"):
            return _lm_response(url, {"data": [{"id": "qwen3.6-27b"}]})
        if url.endswith("/api/v0/models"):
            return httpx.Response(404, request=httpx.Request("GET", url))
        return await _real_async_client_get(self, url, *a, **kw)

    mocker.patch("httpx.AsyncClient.get", new=_fake_get)

    response = await client.get("/api/lm-models", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["models"] == ["qwen3.6-27b"]
    assert body["embedding_model_ids"] == []
