"""LLM calls metrics and pricing rollups."""
from __future__ import annotations

from typing import Any

from orchestrator.core.pricing import est_cost_avoided_usd


async def record_llm_call(
    db: Any,
    *,
    plan_id: str | None,
    task_id: str | None,
    call_site: str,
    provider: str,
    model: str,
    prompt_chars: int,
    response_chars: int,
    duration_ms: int,
    source: str = "brain",
) -> None:
    """Record a single LLM call."""
    pass


async def plan_token_usage(db: Any, plan_id: str) -> dict:
    """Aggregate plan token usage and estimate cost avoided."""
    return {}
