"""Smoke test that the app wires the chain-based router + event bus."""

# ruff: noqa: S101, EM101, PT018

from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.main import app


async def test_router_uses_chain_resolver_and_bus(
    test_settings: Settings,
) -> None:
    async with app.router.lifespan_context(app):
        router = app.state.llm_router
        assert router._event_bus is app.state.event_bus
        # resolve_chain returns a list for a known call-site
        chain = await router._resolve_chain("plan_spec", None)
        assert isinstance(chain, list) and chain


async def test_orchestrator_can_read_spec_docs(test_settings: Settings) -> None:
    """The planner resolves ``plans.spec_path`` by reading the repo.

    Wiring-only seam: every end of the submit -> plan carrier can be correct
    while the orchestrator is built without a reader, in which case planning
    fails closed on every submitted spec.
    """
    async with app.router.lifespan_context(app):
        assert app.state.orchestrator._spec_reader is app.state.brainstorm
