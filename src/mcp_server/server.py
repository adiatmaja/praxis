"""Praxis MCP server: tool implementations + FastMCP registration.

Each ``*_impl`` function takes a PraxisClient and is independently testable.
The FastMCP tool wrappers (registered at module import) build a client from env
and delegate. Tools never raise to the MCP client; client errors are caught and
returned as ``{"error": code, "message": ...}`` so the brain can react.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from mcp_server.client import PraxisClient, PraxisClientError
from orchestrator.core import status_vocab


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
    local_context: str | None = None,
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
    if local_context is not None:
        payload["local_context"] = local_context
    if expected_base_sha is not None:
        payload["expected_base_sha"] = expected_base_sha
    try:
        result = cast(dict[str, Any], await client.post("/api/dispatch", payload))
    except PraxisClientError as exc:
        return _error(exc)
    # The API derives dashboard_url from its own bind port, which is not the
    # externally reachable URL. The MCP client's base_url is authoritative, so
    # override it here (consistent with poll_task/poll_plan). Only override when
    # the client exposes a base_url; otherwise keep the API's value.
    dash = _dashboard_url(client)
    if dash and isinstance(result, dict) and "dashboard_url" in result:
        result["dashboard_url"] = dash
    return result


async def execute_plan_impl(
    client: Any,
    repo_url: str,
    plan: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    local_context: str | None = None,
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
    if local_context is not None:
        payload["local_context"] = local_context
    if expected_base_sha is not None:
        payload["expected_base_sha"] = expected_base_sha
    try:
        result = cast(dict[str, Any], await client.post("/api/execute-plan", payload))
    except PraxisClientError as exc:
        return _error(exc)
    # See dispatch_task_impl: the API's dashboard_url uses its bind port, not the
    # externally reachable URL. Override with the MCP client's authoritative base
    # when known; otherwise keep the API's value.
    dash = _dashboard_url(client)
    if dash and isinstance(result, dict) and "dashboard_url" in result:
        result["dashboard_url"] = dash
    return result


async def poll_task_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Return the current status, PR URL, and review of a dispatched task.

    When a task has been reviewed and its PR approved, the status is returned
    as ``awaiting_merge`` (mapped from the internal ``passed`` state) and
    ``verdict`` is set to ``"pass"``.  The caller should surface the ``pr_url``
    to a human for final merge approval.

    Note: this ``awaiting_merge`` alias is an MCP-surface convenience. The raw
    REST endpoint ``GET /api/tasks/{id}`` reports the underlying DB status
    ``passed`` for the same state, so a caller polling REST directly must match
    on ``passed`` (not ``awaiting_merge``). Prefer the MCP tools for consistency.
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


_TASK_STATUS_MAP = status_vocab.MCP_STATUS_ALIASES

# Statuses that are gated (passed review but not yet merged into the base branch).
_GATED_STATUSES = status_vocab.GATED_STATUSES

# Terminal statuses that cannot progress further without intervention.
_TERMINAL_STATUSES = status_vocab.TERMINAL_STATUSES


def derive_plan_blocked_state(
    opus_plan_json: str | None,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive a machine-readable summary of any merge-gate stall in a plan.

    Cross-references the ``depends_on`` graph from the serialized ``opus_plan``
    against the live task statuses to find tasks that are pending solely because
    their dependencies are gated at ``passed``/``awaiting_merge`` (i.e. passed
    review but awaiting a human ``approve-merge`` call).

    Args:
        opus_plan_json: The ``plans.opus_plan`` column value (JSON string), or
            ``None`` when the plan is still being decomposed.
        tasks: Ordered list of task rows from ``GET /api/plans/{id}/tasks``.

    Returns:
        A dict with:

        - ``gated_task_ids``: list of task ids sitting at ``passed``/
          ``awaiting_merge``.
        - ``blocked_by_gate``: list of ``{task_id, title, blocked_by}`` for
          pending tasks whose every unmet dependency is gated (not merely
          absent).
        - ``action_required``: ``"approve_merge"`` when at least one pending
          task is blocked purely by gated deps, else ``None``.
        - ``hint``: human-readable explanation string, or ``None``.
    """
    if not opus_plan_json or not tasks:
        return {
            "gated_task_ids": [],
            "blocked_by_gate": [],
            "action_required": None,
            "hint": None,
        }

    try:
        opus_plan = json.loads(opus_plan_json)
    except (ValueError, TypeError):
        return {
            "gated_task_ids": [],
            "blocked_by_gate": [],
            "action_required": None,
            "hint": None,
        }

    plan_tasks: list[dict[str, Any]] = opus_plan.get("tasks", [])

    # Build slug -> task-row mapping (ordered by rowid, same order as opus_plan).
    slug_to_row: dict[str, dict[str, Any]] = {}
    for index, plan_task in enumerate(plan_tasks):
        slug = plan_task.get("slug", "")
        if index < len(tasks):
            slug_to_row[slug] = tasks[index]

    # Identify gated tasks: passed review but not yet merged.
    gated_task_ids: list[str] = [
        row["id"]
        for row in slug_to_row.values()
        if row.get("status") in _GATED_STATUSES and row.get("id")
    ]
    gated_slugs: set[str] = {
        slug
        for slug, row in slug_to_row.items()
        if row.get("status") in _GATED_STATUSES
    }

    # Find pending tasks blocked exclusively by gated deps (not failed/missing deps).
    blocked_by_gate: list[dict[str, Any]] = []
    for plan_task in plan_tasks:
        slug = plan_task.get("slug", "")
        row = slug_to_row.get(slug)
        if row is None or row.get("status") != "pending":
            continue
        deps: list[str] = plan_task.get("depends_on") or []
        if not deps:
            continue
        unmet_deps = [
            d for d in deps if slug_to_row.get(d, {}).get("status") != "merged"
        ]
        if not unmet_deps:
            continue
        # Only flag as "blocked by gate" when ALL unmet deps are gated (not failed/missing).
        all_gated = all(d in gated_slugs for d in unmet_deps)
        if all_gated:
            blocking_ids = [
                slug_to_row[d]["id"] for d in unmet_deps if d in slug_to_row
            ]
            blocking_prs = [
                slug_to_row[d].get("pr_url")
                for d in unmet_deps
                if d in slug_to_row and slug_to_row[d].get("pr_url")
            ]
            blocked_by_gate.append(
                {
                    "task_id": row.get("id"),
                    "title": row.get("title"),
                    "blocked_by_task_ids": blocking_ids,
                    "blocked_by_pr_urls": blocking_prs,
                }
            )

    action_required: str | None = None
    hint: str | None = None
    if blocked_by_gate:
        action_required = "approve_merge"
        ids_str = ", ".join(b["task_id"] for b in blocked_by_gate if b.get("task_id"))
        gate_ids_str = ", ".join(gated_task_ids)
        hint = (
            f"{len(blocked_by_gate)} task(s) are pending because their dependencies "
            f"({gate_ids_str}) passed review but have not been merged yet. "
            f"Blocked tasks: {ids_str}. "
            f"Call approve-merge on the gated tasks (or POST /api/plans/{{plan_id}}/approve-merges) "
            f"to unblock the next wave."
        )

    return {
        "gated_task_ids": gated_task_ids,
        "blocked_by_gate": blocked_by_gate,
        "action_required": action_required,
        "hint": hint,
    }


def derive_terminal_incomplete_state(
    plan_status: str | None,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Detect when a plan is terminal but not fully merged (some tasks failed).

    When at least one task has failed and no tasks are still in progress, the
    plan is effectively stalled. If the plan has a ``plan_branch_name`` the
    orchestrator may have already opened an integration PR for successfully
    merged tasks; the caller should check the dashboard for that URL.

    Args:
        plan_status: Current plan status string (e.g. ``"active"``, ``"failed"``).
        tasks: Task rows for the plan.

    Returns:
        A dict with:

        - ``terminal_incomplete``: ``True`` when some tasks failed and none are
          actively making progress.
        - ``failed_count``: number of tasks with ``status="failed"``.
        - ``merged_count``: number of tasks with ``status="merged"``.
        - ``hint``: human-readable explanation, or ``None``.
    """
    if not tasks:
        return {
            "terminal_incomplete": False,
            "failed_count": 0,
            "merged_count": 0,
            "hint": None,
        }

    failed_count = sum(1 for t in tasks if t.get("status") == "failed")
    merged_count = sum(1 for t in tasks if t.get("status") == "merged")
    in_progress_count = sum(
        1
        for t in tasks
        if t.get("status") in {"in_progress", "reviewing", "needs_clarification"}
    )

    terminal_incomplete = (
        failed_count > 0 and in_progress_count == 0 and merged_count > 0
    )

    hint: str | None = None
    if terminal_incomplete:
        hint = (
            f"{failed_count} task(s) failed; {merged_count} task(s) merged. "
            "The orchestrator may have opened an integration PR for the merged tasks. "
            "Check the dashboard_url for the integration PR and consider merging partial "
            "progress, then re-plan the failed tasks."
        )

    return {
        "terminal_incomplete": terminal_incomplete,
        "failed_count": failed_count,
        "merged_count": merged_count,
        "hint": hint,
    }


async def poll_plan_impl(client: Any, plan_id: str) -> dict[str, Any]:
    """Return the plan status plus a one-line summary of every task in the plan.

    The response is enriched with two diagnostic fields:

    - ``merge_gate``: populated when pending tasks are stalled because their
      dependencies passed review but have not been merged yet.  Contains
      ``action_required="approve_merge"``, ``gated_task_ids``, ``blocked_by_gate``
      (list of blocked task summaries), and a human-readable ``hint``.

    - ``terminal_incomplete``: populated when some tasks failed while others
      merged successfully (partial plan completion).  Contains ``failed_count``,
      ``merged_count``, and a ``hint`` suggesting the operator check for an
      integration PR on the dashboard.
    """
    try:
        plan_data = await client.get(f"/api/plans/{plan_id}")
    except PraxisClientError as exc:
        return _error(exc)
    try:
        tasks_data = await client.get(f"/api/plans/{plan_id}/tasks")
    except PraxisClientError as exc:
        return _error(exc)
    tasks: list[dict[str, Any]] = tasks_data if isinstance(tasks_data, list) else []
    opus_plan_json: str | None = plan_data.get("opus_plan")
    merge_gate = derive_plan_blocked_state(opus_plan_json, tasks)
    term = derive_terminal_incomplete_state(plan_data.get("status"), tasks)
    return {
        "plan_id": plan_id,
        "status": plan_data.get("status"),
        "error": plan_data.get("error"),
        "token_usage": plan_data.get("token_usage"),
        "task_count": len(tasks),
        "tasks": [
            {
                "task_id": t.get("id"),
                "title": t.get("title"),
                "status": _TASK_STATUS_MAP.get(t.get("status", ""), t.get("status")),
                "pr_url": t.get("pr_url"),
            }
            for t in tasks
        ],
        "merge_gate": merge_gate,
        "terminal_incomplete": term,
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
            # Defensive .get(): MCP tools must never raise on a malformed row
            # (only PraxisClientError is caught), so a missing key returns null.
            return {
                "project_id": row.get("id"),
                "name": row.get("name"),
                "model": row.get("model_name"),
                "harness": row.get("harness"),
                "default_branch": row.get("default_branch"),
                "approval_gate": row.get("approval_gate"),
            }
    return {"project": None}


async def list_projects_impl(client: Any) -> dict[str, Any]:
    """Return a slim list of all configured projects."""
    try:
        projects = await client.get("/api/projects")
    except PraxisClientError as exc:
        return _error(exc)
    rows = projects if isinstance(projects, list) else []
    return {
        "projects": [
            # Defensive .get(): a malformed row must not raise out of an MCP tool.
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "repo_url": row.get("repo_url"),
                "model": row.get("model_name"),
                "harness": row.get("harness"),
            }
            for row in rows
        ]
    }


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
    local_context: str | None = None,
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

    local_context: Optional NON-committed context the worker cannot see from a
    git clone (gitignored config shapes, user-scope conventions). Self-contained
    inline text, never a "read file X" pointer. Prefer env var NAMES/shapes over
    live values: the worker writes code, it does not run it.

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
        local_context=local_context,
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
    local_context: str | None = None,
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

    local_context: Optional NON-committed context the worker cannot see from a
    git clone (gitignored config shapes, user-scope conventions). Self-contained
    inline text, never a "read file X" pointer. Prefer env var NAMES/shapes over
    live values: the worker writes code, it does not run it.

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
        local_context=local_context,
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


@mcp.tool()
async def list_projects() -> dict[str, Any]:
    """List all repos Praxis knows, each with its configured model + harness."""
    return await list_projects_impl(PraxisClient.from_env())


@mcp.resource("praxis://guide/orchestration")
def orchestration_guide() -> str:
    """Workflow guide for an agent orchestrating Praxis over MCP.

    Covers when to delegate to Praxis and how to drive its tools: tool
    selection, what context to pass, polling cadence, task statuses, and
    troubleshooting. For live provider/model state, call list_providers.
    """
    return load_orchestration_guide()
