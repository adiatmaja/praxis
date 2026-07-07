"""Praxis MCP server: tool implementations + FastMCP registration.

Each ``*_impl`` function takes a PraxisClient and is independently testable.
The FastMCP tool wrappers (registered at module import) build a client from env
and delegate. Tools never raise to the MCP client; client errors are caught and
returned as ``{"error": code, "message": ...}`` so the brain can react.
"""

from __future__ import annotations

from importlib import resources
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from mcp_server.client import PraxisClient, PraxisClientError


def load_orchestration_guide() -> str:
    """Read the packaged orchestration-guide markdown, CWD-independent.

    Returns:
        The full markdown content of the orchestration guide.
    """
    return (
        resources.files("mcp_server.resources")
        .joinpath("orchestration_guide.md")
        .read_text(encoding="utf-8")
    )


def _error(exc: PraxisClientError) -> dict[str, Any]:
    return {"error": exc.code, "message": exc.message}


async def dispatch_task_impl(
    client: Any,
    repo_url: str,
    instructions: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    expected_base_sha: str | None = None,
) -> dict[str, Any]:
    """Dispatch a single implementation task to a non-Anthropic worker model."""
    payload: dict[str, Any] = {
        "repo_url": repo_url,
        "instructions": instructions,
        "model": model,
    }
    if harness is not None:
        payload["harness"] = harness
    if branch is not None:
        payload["branch"] = branch
    if context is not None:
        payload["context"] = context
    if expected_base_sha is not None:
        payload["expected_base_sha"] = expected_base_sha
    try:
        return cast(dict[str, Any], await client.post("/api/dispatch", payload))
    except PraxisClientError as exc:
        return _error(exc)


async def execute_plan_impl(
    client: Any,
    repo_url: str,
    plan: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    expected_base_sha: str | None = None,
) -> dict[str, Any]:
    """Submit a full, externally-authored plan for capability-aware execution."""
    payload: dict[str, Any] = {
        "repo_url": repo_url,
        "plan": plan,
        "model": model,
    }
    if harness is not None:
        payload["harness"] = harness
    if branch is not None:
        payload["branch"] = branch
    if context is not None:
        payload["context"] = context
    if expected_base_sha is not None:
        payload["expected_base_sha"] = expected_base_sha
    try:
        return cast(dict[str, Any], await client.post("/api/execute-plan", payload))
    except PraxisClientError as exc:
        return _error(exc)


async def poll_task_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Return the current status, PR URL, and review of a dispatched task.

    When a task has been reviewed and its PR approved, the status is returned
    as ``awaiting_merge`` (mapped from the internal ``passed`` state) and
    ``verdict`` is set to ``"pass"``.  The caller should surface the ``pr_url``
    to a human for final merge approval.
    """
    try:
        data = await client.get(f"/api/tasks/{task_id}")
    except PraxisClientError as exc:
        return _error(exc)
    task = data.get("task", {})
    raw_status = task.get("status")
    awaiting = raw_status == "passed"
    if raw_status == "needs_clarification":
        return {
            "task_id": task_id,
            "status": "awaiting_clarification",
            "question": task.get("clarification_question") or "",
            "pr_url": task.get("pr_url"),
            "branch": task.get("branch_name"),
            "dashboard_url": _dashboard_url(client),
        }
    return {
        "task_id": task_id,
        "status": "awaiting_merge" if awaiting else raw_status,
        "pr_url": task.get("pr_url"),
        "review": task.get("review_feedback"),
        "branch": task.get("branch_name"),
        "verdict": "pass" if awaiting else None,
        "dashboard_url": _dashboard_url(client),
    }


_TASK_STATUS_MAP: dict[str, str] = {
    "passed": "awaiting_merge",
    "needs_clarification": "awaiting_clarification",
}


async def poll_plan_impl(client: Any, plan_id: str) -> dict[str, Any]:
    """Return the plan status plus a one-line summary of every task in the plan."""
    try:
        plan_data = await client.get(f"/api/plans/{plan_id}")
    except PraxisClientError as exc:
        return _error(exc)
    try:
        tasks_data = await client.get(f"/api/plans/{plan_id}/tasks")
    except PraxisClientError as exc:
        return _error(exc)
    tasks: list[dict[str, Any]] = tasks_data if isinstance(tasks_data, list) else []
    return {
        "plan_id": plan_id,
        "status": plan_data.get("status"),
        "error": plan_data.get("error"),
        "task_count": len(tasks),
        "tasks": [
            {
                "task_id": t["id"],
                "title": t.get("title"),
                "status": _TASK_STATUS_MAP.get(t.get("status", ""), t.get("status")),
                "pr_url": t.get("pr_url"),
            }
            for t in tasks
        ],
        "dashboard_url": _dashboard_url(client),
    }


async def list_providers_impl(client: Any) -> dict[str, Any]:
    """List brain providers and the worker models available to dispatch to."""
    try:
        status_data = await client.get("/api/status")
        models_data = await client.get("/api/lm-models")
    except PraxisClientError as exc:
        return _error(exc)
    return {
        "brain_providers": status_data.get("providers", []),
        "worker_models": models_data.get("models", []),
        "lm_studio_url": status_data.get("lm_studio_url"),
        "lm_studio_connected": models_data.get("connected", False),
    }


async def get_project_impl(client: Any, repo_url: str) -> dict[str, Any]:
    """Return the project config for a given repo_url (or null if unknown)."""
    try:
        projects = await client.get("/api/projects")
    except PraxisClientError as exc:
        return _error(exc)
    rows = projects if isinstance(projects, list) else []
    for row in rows:
        if row.get("repo_url") == repo_url:
            return {
                "project_id": row["id"],
                "name": row["name"],
                "model": row["model_name"],
                "harness": row["harness"],
                "default_branch": row["default_branch"],
                "approval_gate": row["approval_gate"],
            }
    return {"project": None}


async def get_task_logs_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Return concatenated agent-run logs for a task (inline failure triage)."""
    try:
        data = await client.get(f"/api/tasks/{task_id}")
    except PraxisClientError as exc:
        return _error(exc)
    runs = data.get("runs", [])
    logs = "".join(str(run.get("logs") or "") for run in runs)
    return {"task_id": task_id, "logs": logs}


async def cancel_task_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Stop a running task's agent containers and mark it failed."""
    try:
        data = await client.post(f"/api/tasks/{task_id}/stop")
    except PraxisClientError as exc:
        return _error(exc)
    return {"status": "cancelled", "stopped": data.get("stopped", 0)}


def _dashboard_url(client: Any) -> str:
    base = getattr(client, "base_url", "").rstrip("/")
    return f"{base}/" if base else ""


# --- FastMCP registration -------------------------------------------------

mcp = FastMCP("praxis")


@mcp.tool()
async def dispatch_task(
    repo_url: str,
    instructions: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    expected_base_sha: str | None = None,
) -> dict[str, Any]:
    """Dispatch an implementation task to a non-Anthropic worker model inside Praxis.

    Returns a handle: {task_id, plan_id, project_id, status, dashboard_url}.
    Poll with poll_task. Praxis always runs its own review before merge.

    context: Optional curated context to brief the worker: task-relevant project
    memory, conventions, and architecture notes that help implement THIS task.
    Pass a focused slice, not your whole memory tree. Do NOT include secrets,
    tokens, or .env values - they are redacted server-side, but keep them out
    anyway.

    expected_base_sha: origin base sha you validated locally; server rejects a mismatch.
    """
    return await dispatch_task_impl(
        PraxisClient.from_env(),
        repo_url=repo_url,
        instructions=instructions,
        model=model,
        harness=harness,
        branch=branch,
        context=context,
        expected_base_sha=expected_base_sha,
    )


@mcp.tool()
async def execute_plan(
    repo_url: str,
    plan: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    expected_base_sha: str | None = None,
) -> dict[str, Any]:
    """Execute a full, externally-authored implementation plan on a repo.

    Praxis accepts the plan and returns immediately with {plan_id, project_id,
    dashboard_url, status="decomposing"}. Decomposition (a multi-minute brain
    call) then runs asynchronously in the orchestration loop; the task graph
    and per-task PRs appear shortly after. Watch the dashboard_url, or poll the
    plan's tasks as they are created. Pass the FULL plan text. Use this (not
    dispatch_task) when you already have a multi-step plan.

    context: Optional curated, secret-scrubbed reference text for the worker.

    expected_base_sha: origin base sha you validated locally; server rejects a mismatch.
    """
    return await execute_plan_impl(
        PraxisClient.from_env(),
        repo_url=repo_url,
        plan=plan,
        model=model,
        harness=harness,
        branch=branch,
        context=context,
        expected_base_sha=expected_base_sha,
    )


@mcp.tool()
async def poll_task(task_id: str) -> dict[str, Any]:
    """Get the status, PR URL, and review of a dispatched task.

    Returns ``status="awaiting_merge"`` (with ``verdict="pass"``) when the
    task's PR has passed review and is parked for human approval.  Relay
    ``pr_url`` to the user so they can approve and merge the PR themselves.
    """
    return await poll_task_impl(PraxisClient.from_env(), task_id=task_id)


@mcp.tool()
async def poll_plan(plan_id: str) -> dict[str, Any]:
    """Get the status of a plan and a one-line summary of each of its tasks.

    Returns the plan's current status and a list of task summaries, each
    containing task_id, title, status, and pr_url.  Tasks with status
    ``awaiting_merge`` have passed review and are parked for human PR approval;
    relay the pr_url to the user so they can approve and merge.  Tasks with
    status ``awaiting_clarification`` are blocked on a question.

    Use this tool to watch an ``execute_plan`` submission progress: call with
    the plan_id returned by execute_plan and poll until the plan status is
    ``completed`` or all tasks are in a terminal state.
    """
    return await poll_plan_impl(PraxisClient.from_env(), plan_id=plan_id)


@mcp.tool()
async def list_providers() -> dict[str, Any]:
    """List brain providers and the worker models available to dispatch to."""
    return await list_providers_impl(PraxisClient.from_env())


@mcp.tool()
async def get_task_logs(task_id: str) -> dict[str, Any]:
    """Return the agent-run logs for a task (for diagnosing a wedged/failed run)."""
    return await get_task_logs_impl(PraxisClient.from_env(), task_id=task_id)


@mcp.tool()
async def cancel_task(task_id: str) -> dict[str, Any]:
    """Stop a running task and mark it failed."""
    return await cancel_task_impl(PraxisClient.from_env(), task_id=task_id)


@mcp.tool()
async def get_project(repo_url: str) -> dict[str, Any]:
    """Read a repo's configured worker model, harness, and settings (or null if unknown)."""
    return await get_project_impl(PraxisClient.from_env(), repo_url=repo_url)


@mcp.resource("praxis://guide/orchestration")
def orchestration_guide() -> str:
    """Workflow guide for an agent orchestrating Praxis over MCP.

    Covers when to delegate to Praxis and how to drive its tools: tool
    selection, what context to pass, polling cadence, task statuses, and
    troubleshooting. For live provider/model state, call list_providers.
    """
    return load_orchestration_guide()
