"""A provider-error classification must NAME the signal it matched.

Round 10 recorded this line from a live run and could not tell what it meant::

    Task ... worker provider/gateway error (streak 1/5); re-queued without
    consuming a retry attempt: Agent finished with status failed

"Agent finished with status failed" is the worker's REASON, which every
non-zero exit produces. The classification came from a substring scan over
the whole container log, and the line named neither the signal that matched
nor where. So an operator reading it could not tell a real gateway timeout
from a false positive, and the finding went into the ledger as "a bare
``failed`` is read as a provider error" with no way to check.

Two things are pinned here. The classifier returns the signal AND the line it
matched on, and both re-queue seats (callback and reconcile) carry them into
the log line, the event and the stored feedback. And the entrypoint's OWN
callback report (``WARNING: callback attempt 1/5 failed (HTTP 503)``) is not
evidence about the model endpoint: it is the worker failing to reach the
ORCHESTRATOR, and the substring ``HTTP 503`` in it used to classify the run
as a worker-endpoint outage.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.provider_errors import (
    ProviderSignal,
    find_provider_signal,
    is_provider_error,
    provider_error_feedback,
)
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


_GATEWAY_LINE = "Error: Forbidden: request was blocked by a gateway or proxy"

#: The entrypoint's own wording, verbatim from ``docker/*/entrypoint.sh``.
_CALLBACK_REPORT = "WARNING: callback attempt 1/5 failed (HTTP 503)"
_CALLBACK_GAVE_UP = (
    "ERROR: callback failed after 5 attempts; orchestrator will reconcile"
)


def test_find_provider_signal_names_signal_and_line() -> None:
    log = "opencode run\nsome tool output\n" + _GATEWAY_LINE + "\nexit 1\n"
    found = find_provider_signal(log)
    assert found == ProviderSignal(
        signal="Forbidden: request was blocked by a gateway or proxy",
        line=_GATEWAY_LINE,
    )
    assert is_provider_error(log)


def test_find_provider_signal_is_none_for_ordinary_output() -> None:
    assert find_provider_signal("pytest exited 1: assert 2 == 3") is None
    assert find_provider_signal("") is None


def test_the_entrypoints_own_callback_report_is_not_a_provider_error() -> None:
    """``HTTP 503`` on the callback line is about the ORCHESTRATOR, not the model."""
    log = "worker output\n" + _CALLBACK_REPORT + "\n" + _CALLBACK_GAVE_UP + "\n"
    assert find_provider_signal(log) is None
    assert not is_provider_error(log)


def test_a_real_signal_beside_a_callback_report_still_counts() -> None:
    log = _CALLBACK_REPORT + "\nHTTP 504 Gateway Timeout from upstream\n"
    found = find_provider_signal(log)
    assert found is not None
    assert found.signal == "HTTP 504"
    assert found.line == "HTTP 504 Gateway Timeout from upstream"


def test_a_signal_on_a_line_prefixed_by_a_diff_marker_still_counts() -> None:
    """Only the entrypoint's exact report prefix is excluded, nothing wider."""
    assert find_provider_signal(" WARNING: callback attempt HTTP 503") is not None


def test_feedback_states_the_action_first_and_keeps_the_original_reason() -> None:
    found = ProviderSignal(signal="HTTP 504", line="HTTP 504 Gateway Timeout")
    text = provider_error_feedback(found, "Agent finished with status failed")
    assert text.startswith("Start this task again from the beginning")
    assert "HTTP 504" in text
    assert "HTTP 504 Gateway Timeout" in text
    assert text.endswith("Agent finished with status failed")


def test_feedback_truncates_a_long_evidence_line() -> None:
    found = ProviderSignal(signal="HTTP 502", line="x" * 5000)
    text = provider_error_feedback(found, "reason")
    assert len(text) < 1000


class _Agents:
    def __init__(self, log: str) -> None:
        self.log = log

    def get_container_logs(self, container_id: str, tail: int = 500) -> str:
        return self.log

    def cleanup_container(self, container_id: str) -> None:
        return None


async def _seed_task(db: Database) -> tuple[TaskQueue, str]:
    await db.execute(
        "INSERT OR IGNORE INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT OR IGNORE INTO projects
           (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "qwen", 3),
    )
    tq = TaskQueue(db)
    plan_id = await tq.create_plan("p1", "Build auth")
    await tq.activate_plan(
        plan_id,
        {
            "plan_summary": "auth",
            "plan_slug": "auth",
            "tasks": [
                {
                    "title": "Login",
                    "slug": "auth-login",
                    "description": "Build login",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-09-05-auth",
    )
    tasks = await tq.get_tasks_for_plan(plan_id)
    return tq, str(tasks[0]["id"])


def _drain(events: Any) -> list[dict[str, Any]]:
    published: list[dict[str, Any]] = []
    while not events.empty():
        published.append(events.get_nowait())
    return published


@pytest.mark.integration
async def test_callback_re_queue_names_the_signal_in_log_event_and_feedback(
    client: AsyncClient, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    tq, task_id = await _seed_task(db)
    client.app.state.agent_manager = _Agents(  # type: ignore[attr-defined]
        "tool output\n" + _GATEWAY_LINE + "\n"
    )
    events = client.app.state.event_bus.subscribe()  # type: ignore[attr-defined]
    await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await tq.create_agent_run(task_id, "container-0")

    with caplog.at_level(logging.WARNING, logger="orchestrator.api.internal"):
        resp = await client.post(
            "/api/internal/agent-done",
            headers={"X-Praxis-Callback-Token": "test-auth"},
            json={"task_id": task_id, "run_id": run_id, "status": "failed"},
        )
    assert resp.status_code == 200, resp.text

    row = await tq.get_task(task_id)
    assert row is not None
    assert row["status"] == TaskStatus.PENDING
    feedback = str(row["review_feedback"] or "")
    assert feedback.startswith("Start this task again"), feedback
    assert "blocked by a gateway or proxy" in feedback
    assert feedback.endswith("Agent finished with status failed"), feedback

    event = next(e for e in _drain(events) if e["type"] == "worker_provider_error")
    assert event["signal"] == "Forbidden: request was blocked by a gateway or proxy"
    assert event["evidence"] == _GATEWAY_LINE

    line = next(
        r.getMessage()
        for r in caplog.records
        if "provider/gateway error" in r.getMessage()
    )
    assert "matched" in line.lower(), line
    assert _GATEWAY_LINE in line, line


@pytest.mark.integration
async def test_callback_report_alone_is_an_ordinary_failure_that_spends_an_attempt(
    client: AsyncClient, db: Database
) -> None:
    """The positive control for the exclusion, on the path that misread it."""
    tq, task_id = await _seed_task(db)
    client.app.state.agent_manager = _Agents(  # type: ignore[attr-defined]
        "pytest exited 1\n" + _CALLBACK_REPORT + "\nCallback delivered on attempt 2\n"
    )
    events = client.app.state.event_bus.subscribe()  # type: ignore[attr-defined]
    await tq.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await tq.create_agent_run(task_id, "container-0")
    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "failed"},
    )
    assert resp.status_code == 200, resp.text
    row = await tq.get_task(task_id)
    assert row is not None
    assert int(row["attempt"]) == 2, "an ordinary failure must spend an attempt"
    assert not any(e["type"] == "worker_provider_error" for e in _drain(events))


@pytest.mark.integration
async def test_reconcile_re_queue_names_the_signal_in_the_event(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    tq, task_id = await _seed_task(db)
    bus = EventBus()
    events = bus.subscribe()
    orch = Orchestrator(
        task_queue=tq,
        agent_manager=_Agents(_GATEWAY_LINE),  # type: ignore[arg-type]
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=bus,
    )
    orch._provider_error_backoff = 0.0
    run_id = await tq.create_agent_run(task_id, "container-0")
    run = await tq.get_agent_run(run_id)
    assert run is not None
    with caplog.at_level(logging.WARNING):
        await orch._resolve_failed_run_or_pause(
            dict(run), "Agent finished with status failed", can_retry=True
        )
    event = next(e for e in _drain(events) if e["type"] == "worker_provider_error")
    assert event["signal"] == "Forbidden: request was blocked by a gateway or proxy"
    assert event["evidence"] == _GATEWAY_LINE
    row = await tq.get_task(task_id)
    assert row is not None
    assert str(row["review_feedback"]).startswith("Start this task again")
    line = next(
        r.getMessage()
        for r in caplog.records
        if "provider/gateway error" in r.getMessage()
    )
    assert "matched" in line.lower(), line
    assert _GATEWAY_LINE in line, line
