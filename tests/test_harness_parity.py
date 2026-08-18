"""Contract tests: every harness must DECLARE how it is driven.

The point of these tests is that a new harness cannot be added without
answering the two questions that make delegation predictable: how does it
receive a thinking-effort signal, and does it report token usage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestrator.core.agent_manager import AgentManager, build_spawn_env
from orchestrator.core.harnesses import EFFORT_CHANNELS, REGISTRY
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


@pytest.mark.unit
def test_every_harness_declares_a_known_effort_channel() -> None:
    for harness_id, spec in REGISTRY.items():
        assert spec.effort_channel in EFFORT_CHANNELS, (
            f"{harness_id} declares unknown effort_channel {spec.effort_channel!r}"
        )


@pytest.mark.unit
def test_every_harness_declares_token_reporting() -> None:
    for harness_id, spec in REGISTRY.items():
        assert isinstance(spec.reports_tokens, bool), harness_id


@pytest.mark.unit
def test_declared_channels_match_the_verified_reality() -> None:
    # opencode is driven through an OpenAI-compatible provider config, so the
    # effort is a request parameter we control. agy takes its effort inside the
    # Gemini model string ("Gemini 3.5 Flash (High)") and exposes no separate knob.
    assert REGISTRY["opencode"].effort_channel == "request_option"
    assert REGISTRY["opencode"].reports_tokens is False
    assert REGISTRY["agy"].effort_channel == "model_name"
    assert REGISTRY["agy"].reports_tokens is True


# ---------------------------------------------------------------------------
# Task 4: the configured effort must reach the spawn environment.
# ---------------------------------------------------------------------------


def _spawn_env(harness_id: str, **kwargs: object) -> dict[str, str]:
    return build_spawn_env(
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        container_lm_url="http://host.docker.internal:1234",
        model_name="qwen3.8-27b",
        harness_id=harness_id,
        gh_token="tok",
        callback_url="http://orchestrator:8080/internal/agent-done",
        task_id="task-1",
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.unit
def test_opencode_spawn_env_states_reasoning_effort_explicitly() -> None:
    env = _spawn_env("opencode", reasoning_effort="medium")
    assert env["WORKER_REASONING_EFFORT"] == "medium"


@pytest.mark.unit
def test_opencode_spawn_env_never_omits_the_effort_key() -> None:
    # Silence is the bug this guards: an absent key means MAXIMUM effort.
    env = _spawn_env("opencode")
    assert "WORKER_REASONING_EFFORT" in env
    assert env["WORKER_REASONING_EFFORT"] == "none"


@pytest.mark.unit
def test_agy_spawn_env_omits_the_key_it_cannot_honor() -> None:
    env = _spawn_env("agy", reasoning_effort="high")
    assert "WORKER_REASONING_EFFORT" not in env


# ---------------------------------------------------------------------------
# The wiring test: proves the value flows from AgentManager construction
# (where it is sourced from Settings.worker_reasoning_effort, mirroring how
# git_author_name / gemini_creds_volume / opencode_sessions_volume already
# travel) all the way into the environment dict handed to the Docker SDK.
# A test that only calls build_spawn_env directly (like the three above)
# would pass even if AgentManager never threaded the value through at all.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.detect_context_limit", new_callable=AsyncMock)
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_carries_configured_reasoning_effort_to_container(
    mock_docker: MagicMock, mock_detect: AsyncMock
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = MagicMock(id="wiring-1")
    mock_detect.return_value = None

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        worker_reasoning_effort="medium",
    )
    await manager.spawn_agent(
        task_id="wire-1",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        model_name="qwen3.8-27b",
        callback_url="http://cb/",
        harness="opencode",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["WORKER_REASONING_EFFORT"] == "medium"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_defaults_reasoning_effort_when_unconfigured(
    mock_docker: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = MagicMock(id="wiring-2")

    manager = AgentManager(lm_studio_url="http://localhost:1234", github_token="ghp_x")
    await manager.spawn_agent(
        task_id="wire-2",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        model_name="qwen3.8-27b",
        callback_url="http://cb/",
        harness="opencode",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["WORKER_REASONING_EFFORT"] == "none"


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_agy_omits_effort_even_when_configured(
    mock_docker: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = MagicMock(id="wiring-3")

    manager = AgentManager(
        lm_studio_url="http://localhost:1234",
        github_token="ghp_x",
        worker_reasoning_effort="high",
    )
    await manager.spawn_agent(
        task_id="wire-3",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        model_name="Gemini 3.5 Flash (High)",
        callback_url="http://cb/",
        harness="agy",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert "WORKER_REASONING_EFFORT" not in env


# ---------------------------------------------------------------------------
# Task 6: token telemetry columns. agy can report token usage, OpenCode
# cannot, so agent_runs needs both a value column and a column that
# distinguishes "the harness reported zero" from "this harness cannot
# report" -- an unexplained NULL is the same invisible gap the columns
# exist to close.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_agent_runs_has_token_telemetry_columns(db: Database) -> None:
    cols = await db.fetch_all("SELECT name FROM pragma_table_info('agent_runs')")
    names = {c["name"] for c in cols}
    assert "tokens_used" in names
    assert "tokens_source" in names


# ---------------------------------------------------------------------------
# Task 7: the /internal/agent-done callback accepts and persists token
# telemetry. OpenCode cannot report tokens at all, so a callback with no
# tokens_used field must still succeed -- the columns exist precisely to make
# that "cannot report" state visible, not to make it a failure.
# ---------------------------------------------------------------------------


async def _seed_in_progress_task_with_run(
    db: Database, queue: TaskQueue
) -> tuple[str, str]:
    """Create a user+project+plan+task(in_progress)+run; return (task_id, run_id)."""
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u-tok", "Tok User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, model_name,
            harness, max_retries)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "proj-tok",
            "u-tok",
            "TokProj",
            "https://github.com/o/r",
            "main",
            "qwen3.6-27b",
            "agy",
            3,
        ),
    )
    plan_id = await queue.create_plan("proj-tok", "Tokens")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Tokens",
            "plan_slug": "tokens",
            "tasks": [
                {
                    "title": "Do work",
                    "slug": "do-work",
                    "description": "Do the work",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-08-18-tokens",
    )
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    run_id = await queue.create_agent_run(task_id, "container-tok")
    return task_id, run_id


@pytest.mark.integration
async def test_agent_done_with_tokens_used_persists_harness_source(
    client: AsyncClient, db: Database
) -> None:
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    task_id, run_id = await _seed_in_progress_task_with_run(db, queue)

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "run_id": run_id,
            "status": "completed",
            "tokens_used": 4321,
        },
    )
    assert resp.status_code == 200

    run = await queue.get_agent_run(run_id)
    assert run is not None
    assert run["tokens_used"] == 4321
    assert run["tokens_source"] == "harness"


@pytest.mark.integration
async def test_agent_done_without_tokens_used_persists_unavailable_source(
    client: AsyncClient, db: Database
) -> None:
    """OpenCode cannot report tokens; a callback with no field must still succeed."""
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    task_id, run_id = await _seed_in_progress_task_with_run(db, queue)

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={"task_id": task_id, "run_id": run_id, "status": "completed"},
    )
    assert resp.status_code == 200

    run = await queue.get_agent_run(run_id)
    assert run is not None
    assert run["tokens_used"] is None
    assert run["tokens_source"] == "unavailable"


@pytest.mark.integration
async def test_agent_done_with_zero_tokens_used_is_not_treated_as_missing(
    client: AsyncClient, db: Database
) -> None:
    """A reported 0 is a real count, not a stand-in for "not reported".

    ``if body.tokens_used:`` is falsy for 0 -- exactly the bug this pins.
    """
    queue: TaskQueue = client.app.state.task_queue  # type: ignore[attr-defined]
    task_id, run_id = await _seed_in_progress_task_with_run(db, queue)

    resp = await client.post(
        "/api/internal/agent-done",
        headers={"X-Praxis-Callback-Token": "test-auth"},
        json={
            "task_id": task_id,
            "run_id": run_id,
            "status": "completed",
            "tokens_used": 0,
        },
    )
    assert resp.status_code == 200

    run = await queue.get_agent_run(run_id)
    assert run is not None
    assert run["tokens_used"] == 0
    assert run["tokens_source"] == "harness"
