"""Tests for LLMRouter chain-based fallback."""

# ruff: noqa: S101, EM101, PT018

from __future__ import annotations

import pytest

from orchestrator.core.llm_router import (
    LLMRouter,
    ProviderAuthError,
    ProviderOutputError,
)


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


def _router(chain, execute, bus=None) -> LLMRouter:
    async def resolve_chain(call_site, project_id):
        return chain

    r = LLMRouter(resolve_chain=resolve_chain, lm_studio_url="http://x", event_bus=bus)
    r._execute_one = execute  # type: ignore[assignment]
    return r


async def test_first_entry_used_when_available() -> None:
    async def execute(cfg, prompt, cwd):
        return f"ok:{cfg['model']}"

    r = _router([{"provider": "claude", "model": "a", "effort": None}], execute)
    assert await r.run("plan_spec", "p", None) == "ok:a"


async def test_falls_back_on_unavailability() -> None:
    calls: list[str] = []

    async def execute(cfg, prompt, cwd):
        calls.append(cfg["model"])
        if cfg["model"] == "a":
            raise ProviderAuthError("claude", "claude login")
        return "ok:b"

    bus = _Bus()
    r = _router(
        [
            {"provider": "claude", "model": "a", "effort": None},
            {"provider": "claude", "model": "b", "effort": None},
        ],
        execute,
        bus,
    )
    assert await r.run("plan_spec", "p", None) == "ok:b"
    assert calls == ["a", "b"]
    assert any(e["type"] == "model_fallback" for e in bus.events)


async def test_bad_output_does_not_fall_back() -> None:
    calls: list[str] = []

    async def execute(cfg, prompt, cwd):
        calls.append(cfg["model"])
        raise ProviderOutputError("empty")

    r = _router(
        [
            {"provider": "claude", "model": "a", "effort": None},
            {"provider": "claude", "model": "b", "effort": None},
        ],
        execute,
    )
    with pytest.raises(ProviderOutputError):
        await r.run("plan_spec", "p", None)
    assert calls == ["a"]  # never tried b


async def test_exhausted_chain_raises_last_error() -> None:
    async def execute(cfg, prompt, cwd):
        raise ProviderAuthError("claude", "claude login")

    r = _router(
        [
            {"provider": "claude", "model": "a", "effort": None},
            {"provider": "claude", "model": "b", "effort": None},
        ],
        execute,
    )
    with pytest.raises(ProviderAuthError):
        await r.run("plan_spec", "p", None)
