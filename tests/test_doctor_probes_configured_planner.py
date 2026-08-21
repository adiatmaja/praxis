"""``praxis doctor`` must probe the planner the LOOP will use, not a default.

The planner row is the one doctor check that spends a real model call, and it
used to spend it on ``claude -p`` with no ``--model`` at all: the subscription
CLI answered on its own default model while the loop resolves the ``plan_spec``
call site through ``EffectiveSettings.call_site_chain`` (the exact callable
``main.py`` hands to ``LLMRouter(resolve_chain=...)``) to a concrete
``{provider, model, effort}``. A green about a model nobody will call is the
silent lie this endpoint exists to remove.

Two halves are load bearing and each is pinned separately below:

* the RESOLUTION comes from ``call_site_chain``, so the doctor and the loop
  cannot disagree about which planner is configured;
* the EXECUTION goes through ``llm_router.build_argv``, so the flags the probe
  runs are the flags the loop runs (``--effort``, never
  ``--reasoning-effort``).

Every assertion here is on the argv the probe actually spawned or on the text
of the rendered row. Asserting that "the probe ran" proves nothing: a stubbed
probe returns the same sentinel whatever it is handed.
"""
# ruff: noqa: S101

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from httpx import AsyncClient

# Bound at import time, BEFORE conftest's `no_live_planner_round_trip` swaps the
# module attribute for a stub. These tests exercise the real body against a fake
# subprocess, which is that fixture's documented escape hatch.
from orchestrator.api.system import probe_provider_roundtrip as real_roundtrip
from tests.test_api_doctor import (
    _FakeImage,
    _install_fake_docker,
    _install_fake_worker_endpoint,
)


#: A model string no CLI would ever default to, so an argv carrying it can only
#: have come from the configured chain.
_PLANNER_MODEL = "claude-not-the-cli-default-9"
_PLANNER_EFFORT = "high"


def _rows(body: dict) -> dict[str, dict[str, Any]]:
    return {check["check_id"]: check for check in body["checks"]}


class _FakeProc:
    """A `claude -p` that answers the sentinel on stdout and exits 0."""

    def __init__(self, stdout: bytes = b"PONG\n", returncode: int = 0) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self.killed = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002 - matches asyncio's own kwarg name
        return self._stdout, b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _capture_spawns(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every argv the process layer is asked to spawn."""
    spawned: list[list[str]] = []

    async def _fake_exec(*args: object, **kwargs: object) -> object:
        spawned.append([str(a) for a in args])
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return spawned


def _roundtrip_argv(spawned: list[list[str]]) -> list[str]:
    """The one recorded spawn that is a prompt round trip."""
    prompts = [argv for argv in spawned if "-p" in argv]
    assert prompts, f"no planner round trip was spawned at all; saw {spawned}"
    return prompts[0]


async def _configure_planner(
    client: AsyncClient,
    provider: str = "claude",
    model: str = _PLANNER_MODEL,
    effort: str | None = _PLANNER_EFFORT,
) -> None:
    """Point the ``plan`` role at one named model, the way an operator would.

    Writes the same two ``settings_overrides`` rows
    ``EffectiveSettings.registered_models`` and ``role_chains`` read, so the
    resolution under test is the production one, not a stub.
    """
    es = client.app.state.effective_settings
    await es.set_override(
        "models.registry",
        json.dumps(
            [
                {
                    "name": "planner-under-test",
                    "provider": provider,
                    "model": model,
                    "effort": effort,
                }
            ]
        ),
    )
    await es.set_override("models.roles", json.dumps({"plan": ["planner-under-test"]}))


def _arm_real_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real round-trip body, with the CLI resolved and auth stubbed."""
    import orchestrator.api.system as sys_mod
    from orchestrator.api import doctor as doctor_api

    sys_mod._roundtrip_probe_cache.clear()
    sys_mod._provider_probe_cache.clear()
    monkeypatch.setattr(sys_mod.shutil, "which", lambda name: name)
    monkeypatch.setattr(doctor_api, "probe_provider_roundtrip", real_roundtrip)

    async def _authenticated(name: str) -> dict[str, Any]:
        return {"cli_available": True, "authenticated": True}

    monkeypatch.setattr(doctor_api, "_probe_provider", _authenticated)


def _quiet_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the Docker and LM Studio gathering so nothing waits on real IO."""
    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.8-27b"])


@pytest.mark.integration
async def test_the_round_trip_runs_the_configured_planner_model(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect itself: the probe used the CLI's default model, not this one.

    The assertion is on the ARGV the probe spawned. A test that only checked
    "a round trip happened" passes against the hardcoded
    ``probe_provider_roundtrip("claude")`` too, because a stub answers the same
    sentinel whatever model it was never told about.
    """
    _quiet_environment(monkeypatch)
    await _configure_planner(client)
    _arm_real_round_trip(monkeypatch)
    spawned = _capture_spawns(monkeypatch)

    await client.get("/api/doctor", headers=auth_headers)

    argv = _roundtrip_argv(spawned)
    assert "--model" in argv, f"the round trip passed no model at all: {argv}"
    assert argv[argv.index("--model") + 1] == _PLANNER_MODEL, (
        f"the round trip probed the wrong model: {argv}"
    )


@pytest.mark.integration
async def test_the_round_trip_carries_the_resolved_effort_on_the_claude_flag(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Effort is part of what the loop will call, and the flag is ``--effort``.

    ``--reasoning-effort`` is the name that reads right and does not exist, so
    the spelling is asserted, not just the value.
    """
    _quiet_environment(monkeypatch)
    await _configure_planner(client)
    _arm_real_round_trip(monkeypatch)
    spawned = _capture_spawns(monkeypatch)

    await client.get("/api/doctor", headers=auth_headers)

    argv = _roundtrip_argv(spawned)
    assert "--reasoning-effort" not in argv
    assert "--effort" in argv, f"the resolved effort never reached the CLI: {argv}"
    assert argv[argv.index("--effort") + 1] == _PLANNER_EFFORT


@pytest.mark.integration
async def test_the_planner_row_names_the_provider_and_model_it_probed(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A green that does not name what it checked is how this defect survived."""
    _quiet_environment(monkeypatch)
    await _configure_planner(client)
    _arm_real_round_trip(monkeypatch)
    _capture_spawns(monkeypatch)

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["planner_cli"]
    assert row["status"] == "green"
    assert "claude" in row["detail"]
    assert _PLANNER_MODEL in row["detail"], (
        f"the row does not say which model it probed: {row['detail']!r}"
    )


@pytest.mark.integration
async def test_a_failed_resolution_ambers_the_row_and_probes_nothing(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unobtainable fact is not a verdict.

    If the configured planner cannot be resolved, the row must say so and name
    the failure. Probing "claude" anyway would report on something the operator
    never configured, which is the original defect wearing a different hat.
    """
    _quiet_environment(monkeypatch)
    _arm_real_round_trip(monkeypatch)
    spawned = _capture_spawns(monkeypatch)

    async def _boom(call_site: str, project_id: str | None) -> list[dict[str, Any]]:
        message = "the model registry is unreadable"
        raise RuntimeError(message)

    monkeypatch.setattr(client.app.state.effective_settings, "call_site_chain", _boom)

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["planner_cli"]
    assert row["status"] == "amber"
    assert "not checked" in row["detail"]
    assert "the model registry is unreadable" in row["detail"]
    assert not [argv for argv in spawned if "-p" in argv], (
        f"a planner nobody could resolve was probed anyway: {spawned}"
    )


@pytest.mark.integration
async def test_a_non_cli_planner_is_not_reported_as_a_missing_cli(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``local`` is a working planner provider with no binary on PATH.

    Probing PATH for it yields ``cli_available=False``, which renders "planner
    CLI not found" - an invented RED about a correctly configured install. The
    honest answer is amber naming what was configured and why nothing was run.
    """
    _quiet_environment(monkeypatch)
    await _configure_planner(client, provider="local", model="qwen3.8-27b", effort=None)
    _arm_real_round_trip(monkeypatch)
    spawned = _capture_spawns(monkeypatch)

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["planner_cli"]
    assert row["status"] == "amber"
    assert "local" in row["detail"]
    assert "qwen3.8-27b" in row["detail"]
    assert not [argv for argv in spawned if "-p" in argv], (
        f"a non-CLI provider was probed as a CLI anyway: {spawned}"
    )


@pytest.mark.integration
async def test_the_round_trip_cache_does_not_outlive_the_model_it_probed(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconfiguring the planner must not be answered from the old model's cache.

    The cache exists to stop ``/api/doctor`` billing a call per page view. Keyed
    by provider name alone, it also answers for a model that was never probed,
    which is a green about the wrong thing that survives until the TTL expires.
    """
    _quiet_environment(monkeypatch)
    _arm_real_round_trip(monkeypatch)
    spawned = _capture_spawns(monkeypatch)

    await _configure_planner(client, model="claude-first-model-1")
    await client.get("/api/doctor", headers=auth_headers)
    await _configure_planner(client, model="claude-second-model-2")
    await client.get("/api/doctor", headers=auth_headers)

    probed = [argv for argv in spawned if "-p" in argv]
    models = [argv[argv.index("--model") + 1] for argv in probed if "--model" in argv]
    assert models == ["claude-first-model-1", "claude-second-model-2"], (
        f"the second model was answered from the first one's cache: {models}"
    )
