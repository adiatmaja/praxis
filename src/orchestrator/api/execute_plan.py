"""Ingest an externally-authored plan for async, loop-driven execution.

Unlike /api/dispatch (a single task) or /api/plans/promote (deterministic,
brain-free extraction), this route persists a PENDING plan immediately and
returns; the orchestration loop then runs the brain-driven capability-aware
review that decomposes the plan into leaves, flags tasks too hard for the
local model, and activates the resulting task graph via the existing TaskQueue
path. This avoids the MCP client 30-second timeout that would discard the
completed decomposition when all work was done in one request coroutine.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.core.execute_plan_decompose import branch_slug, normalize_slugs
from orchestrator.core.harnesses import default_harness_id
from orchestrator.models.schemas import ExecutePlanRequest, ExecutePlanResponse


router = APIRouter(tags=["execute-plan"], dependencies=[Depends(verify_token)])


# Keep _normalize_slugs as a re-export so existing tests that import it by the
# old private name continue to work.
_normalize_slugs = normalize_slugs
__all__ = ["_normalize_slugs"]


async def _create_or_reuse_project(
    db: Any, repo_url: str, name: str | None, model: str, harness: str
) -> str:
    """Return an existing project id for the repo, or create one. Mirrors dispatch."""
    user = await db.fetch_one("SELECT id FROM users LIMIT 1")
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No user found. Seed a user first.",
        )
    project = await db.fetch_one(
        "SELECT * FROM projects WHERE repo_url = ? ORDER BY rowid LIMIT 1",
        (repo_url,),
    )
    if project is not None:
        project_id = project["id"]
        await db.execute(
            "UPDATE projects SET model_name = ?, harness = ? WHERE id = ?",
            (model, harness, project_id),
        )
        return str(project_id)

    project_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate,
            model_name, harness)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            user["id"],
            name or repo_url.rstrip("/").split("/")[-1] or "execute-plan-project",
            repo_url,
            "main",
            False,
            model,
            harness,
        ),
    )
    return project_id


@router.post(
    "/execute-plan",
    status_code=status.HTTP_201_CREATED,
    response_model=ExecutePlanResponse,
)
async def execute_plan(request: Request, body: ExecutePlanRequest) -> dict[str, Any]:
    """Persist an external plan for async, loop-driven capability decomposition.

    Returns immediately with ``status="decomposing"`` and a ``plan_id``. The
    orchestration loop picks up the pending plan, runs the brain decomposition,
    and activates the task graph. Watch ``dashboard_url`` to track progress.
    """
    state = request.app.state
    db = state.db
    queue = state.task_queue
    settings = state.settings

    harness = body.harness or default_harness_id()
    project_id = await _create_or_reuse_project(
        db, body.repo_url, None, body.model, harness
    )
    branch_name = body.branch or f"plan/execute-{branch_slug(body.plan)}"
    pending_input = json.dumps(
        {
            "plan": body.plan,
            "model": body.model,
            "context": body.context,
            "branch": branch_name,
        }
    )
    plan_id = await queue.create_pending_execute_plan(project_id, pending_input)

    base_url = f"http://localhost:{getattr(settings, 'port', 8080)}/"
    return {
        "plan_id": plan_id,
        "project_id": project_id,
        "dashboard_url": base_url,
        "status": "decomposing",
    }
