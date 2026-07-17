"""Tests for llm_calls module."""

from __future__ import annotations

import pytest

from orchestrator.core.llm_calls import plan_token_usage, record_llm_call
from orchestrator.database import Database


@pytest.mark.integration
async def test_record_llm_call_inserts_row(db: Database) -> None:
    await record_llm_call(
        db,
        plan_id="plan-1",
        task_id="task-1",
        call_site="test_site",
        provider="test_prov",
        model="test_model",
        prompt_chars=10,
        response_chars=20,
        duration_ms=150,
        source="brain",
    )
    rows = await db.fetch_all("SELECT * FROM llm_calls")
    assert len(rows) == 1
    assert rows[0]["plan_id"] == "plan-1"
    assert rows[0]["task_id"] == "task-1"
    assert rows[0]["prompt_chars"] == 10
    assert rows[0]["response_chars"] == 20
    assert rows[0]["source"] == "brain"


@pytest.mark.integration
async def test_plan_token_usage_aggregates_and_calls_pricing(db: Database) -> None:
    # Insert brain calls
    await record_llm_call(
        db,
        plan_id="plan-2",
        task_id=None,
        call_site="site1",
        provider="openai",
        model="gpt-4",
        prompt_chars=100,
        response_chars=50,
        duration_ms=100,
        source="brain",
    )
    await record_llm_call(
        db,
        plan_id="plan-2",
        task_id=None,
        call_site="site2",
        provider="anthropic",
        model="claude-3-opus",
        prompt_chars=200,
        response_chars=50,
        duration_ms=100,
        source="brain",
    )
    # Insert worker calls
    await record_llm_call(
        db,
        plan_id="plan-2",
        task_id="task-1",
        call_site="site3",
        provider="openai",
        model="gpt-4",
        prompt_chars=1000,
        response_chars=2000,
        duration_ms=200,
        source="worker",
    )

    usage = await plan_token_usage(db, "plan-2")

    assert usage["brain_calls"] == 2
    assert usage["brain_chars"] == 400
    assert usage["worker_chars"] == 3000
    # 3000 chars = 750 tokens
    # openai/gpt-4 rate is 30.0 / 1M tokens
    # est cost = 750 / 1_000_000 * 30.0 = 0.0225
    assert usage["est_api_cost_avoided_usd"] == pytest.approx(0.0225)


@pytest.mark.integration
async def test_plan_token_usage_no_rows(db: Database) -> None:
    usage = await plan_token_usage(db, "plan-empty")
    assert usage == {
        "brain_calls": 0,
        "brain_chars": 0,
        "worker_chars": 0,
        "est_api_cost_avoided_usd": 0.0,
    }
