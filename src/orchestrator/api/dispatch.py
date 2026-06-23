"""MCP single-task dispatch endpoint.

Praxis has no direct single-task creation route; tasks are only created via
plan activation. This route injects a one-task plan so an MCP client can
dispatch implementation work without owning the planning step. The plan is
activated immediately (status ACTIVE), so the orchestration loop picks up the
task on its next pass.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.core.harnesses import default_harness_id
from orchestrator.models.schemas import DispatchRequest, DispatchResponse


router = APIRouter(tags=["dispatch"], dependencies=[Depends(verify_token)])


def _slugify(text: str) -> str:
    """Build a short branch-safe slug from free text plus a uniqueness suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"
    return f"{base}-{uuid.uuid4().hex[:6]}"


@router.post(
    "/dispatch",
    status_code=status.HTTP_201_CREATED,
    response_model=DispatchResponse,
)
async def dispatch_task(request: Request, body: DispatchRequest) -> dict[str, Any]:
    """Create-or-reuse a project, then activate a single-task plan."""

    db = request.app.state.db
    queue = request.app.state.task_queue
    settings = request.app.state.settings

    user = await db.fetch_one("SELECT id FROM users LIMIT 1")
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No user found. Seed a user first.",
        )

    harness = body.harness or default_harness_id()
    project = await db.fetch_one(
        "SELECT * FROM projects WHERE repo_url = ? ORDER BY rowid LIMIT 1",
        (body.repo_url,),
    )
    if project is None:
        project_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO projects
               (id, user_id, name, repo_url, default_branch, approval_gate,
                model_name, harness)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                user["id"],
                body.name or body.repo_url.rstrip("/").split("/")[-1] or "mcp-project",
                body.repo_url,
                "main",
                False,
                body.model,
                harness,
            ),
        )
    else:
        project_id = project["id"]
        await db.execute(
            "UPDATE projects SET model_name = ?, harness = ? WHERE id = ?",
            (body.model, harness, project_id),
        )

    plan_id = await queue.create_plan(project_id, source="mcp")
    slug = _slugify(body.instructions)
    opus_plan = {
        "tasks": [
            {
                "title": body.instructions[:80],
                "description": body.instructions,
                "slug": slug,
                "depends_on": [],
            }
        ]
    }
    branch_name = body.branch or f"plan/mcp-{slug}"
    await queue.activate_plan(plan_id, opus_plan, branch_name)

    tasks = await queue.get_tasks_for_plan(plan_id)
    if not tasks:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Task activation produced no task",
        )

    base_url = f"http://localhost:{getattr(settings, 'port', 8080)}/"
    return {
        "task_id": tasks[0]["id"],
        "plan_id": plan_id,
        "project_id": project_id,
        "status": "queued",
        "dashboard_url": base_url,
    }
