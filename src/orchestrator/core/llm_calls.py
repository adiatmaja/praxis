"""LLM calls metrics and pricing rollups."""

from __future__ import annotations

import datetime
import uuid
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
    call_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.UTC).isoformat()
    await db.execute(
        """
        INSERT INTO llm_calls (
            id, plan_id, task_id, call_site, provider, model,
            prompt_chars, response_chars, duration_ms, source, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            call_id,
            plan_id,
            task_id,
            call_site,
            provider,
            model,
            prompt_chars,
            response_chars,
            duration_ms,
            source,
            now,
        ),
    )


async def plan_token_usage(db: Any, plan_id: str) -> dict:
    """Aggregate plan token usage and estimate cost avoided."""
    rows = await db.fetch_all(
        """
        SELECT source, provider, model, prompt_chars, response_chars
        FROM llm_calls
        WHERE plan_id = ?
        """,
        (plan_id,),
    )

    brain_calls = 0
    brain_chars = 0
    worker_chars = 0
    pricing_rows = []

    for row in rows:
        source = row.get("source")
        provider = row.get("provider", "")
        model = row.get("model", "")

        try:
            p_chars = int(row.get("prompt_chars") or 0)
        except (ValueError, TypeError):
            p_chars = 0

        try:
            r_chars = int(row.get("response_chars") or 0)
        except (ValueError, TypeError):
            r_chars = 0

        chars = p_chars + r_chars

        if source == "brain":
            brain_calls += 1
            brain_chars += chars
        elif source == "worker":
            worker_chars += chars
            pricing_rows.append(
                {
                    "source": "worker",
                    "chars": chars,
                    "provider": provider,
                    "model": model,
                }
            )

    cost_avoided = est_cost_avoided_usd(pricing_rows)

    return {
        "brain_calls": brain_calls,
        "brain_chars": brain_chars,
        "worker_chars": worker_chars,
        "est_api_cost_avoided_usd": cost_avoided,
    }
