"""Smoke test that the app wires the chain-based router + event bus."""

# ruff: noqa: S101, EM101, PT018

from __future__ import annotations

from orchestrator.main import app


async def test_router_uses_chain_resolver_and_bus(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    async with app.router.lifespan_context(app):
        router = app.state.llm_router
        assert router._event_bus is app.state.event_bus
        # resolve_chain returns a list for a known call-site
        chain = await router._resolve_chain("plan_spec", None)
        assert isinstance(chain, list) and chain
