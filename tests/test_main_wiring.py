"""Smoke test that the app wires the chain-based router + event bus."""

# ruff: noqa: S101, EM101, PT018

from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.core.opus_bridge import parking_brain_runner
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


async def test_the_direct_to_router_seats_get_the_parking_bridge(
    test_settings: Settings,
) -> None:
    """The one line every rate-limit test decides for itself: what main wires.

    ``decompose_plan`` and ``triage_leaf`` park ``opus_state`` on a
    subscription throttle only because ``parking_brain_runner`` hands them the
    ``OpusBridge`` instead of the bare ``LLMRouter``, and it picks by TYPE.
    Every test of those two seats builds the orchestrator itself and assigns
    ``_opus`` a router-backed bridge, so all of them stay green if the real app
    wires anything else -- a bridge built without ``router=`` included, since
    that one silently cannot route. Both seats would then stop parking with no
    symptom until a five-hour throttle landed, which is the exact defect they
    exist to prevent.
    """
    async with app.router.lifespan_context(app):
        orchestrator = app.state.orchestrator
        runner = parking_brain_runner(orchestrator._opus, orchestrator._llm_router)
        assert runner is app.state.opus_bridge
        assert runner is not app.state.llm_router
