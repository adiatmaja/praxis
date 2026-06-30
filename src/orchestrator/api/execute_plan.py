"""Ingest an externally-authored plan, capability-review it, and execute it.

Unlike /api/dispatch (a single task) or /api/plans/promote (deterministic,
brain-free extraction), this route runs a brain-driven capability-aware review:
it decomposes the plan into leaves the LOCAL model can each complete, flags
tasks too hard for it, and activates the resulting task graph via the existing
TaskQueue path. Mirrors the dispatch project create/reuse plumbing.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.core.capability_history import summarize_outcomes
from orchestrator.core.context_scrub import scrub_context
from orchestrator.core.harnesses import default_harness_id
from orchestrator.core.plan_derive import slugify
from orchestrator.core.plan_review import (
    PlanReviewError,
    build_review_prompt,
    parse_review_response,
)
from orchestrator.models.schemas import ExecutePlanRequest, ExecutePlanResponse


logger = logging.getLogger(__name__)
router = APIRouter(tags=["execute-plan"], dependencies=[Depends(verify_token)])

# Fraction of the model's context window reserved for a single leaf's context.
# Mirrors the Spec 1 worker-context budget reserve.
_LEAF_BUDGET_FRACTION = 0.4


def _slugify(text: str) -> str:
    """Build a short branch-safe slug from free text plus a uniqueness suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "plan"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def _normalize_slugs(opus_plan: dict[str, Any]) -> None:
    """Add a unique ``slug`` to each task and remap ``depends_on`` ids -> slugs.

    The plan-review brain emits tasks keyed by ``id`` (e.g. "t1") with
    ``depends_on`` referencing those ids, but TaskQueue.activate_plan and
    get_dispatchable_tasks key on ``slug`` (and expect depends_on to hold
    slugs). Without this bridge the dispatch loop raises ``KeyError: 'slug'``.
    """
    id_to_slug: dict[str, str] = {}
    seen: set[str] = set()
    for task in opus_plan["tasks"]:
        slug = slugify(str(task.get("title") or task.get("id") or "task"))
        while slug in seen:
            slug = f"{slug}-{uuid.uuid4().hex[:4]}"
        seen.add(slug)
        task["slug"] = slug
        if "id" in task:
            id_to_slug[str(task["id"])] = slug
    for task in opus_plan["tasks"]:
        deps = task.get("depends_on") or []
        task["depends_on"] = [id_to_slug.get(str(d), str(d)) for d in deps]


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
    """Capability-review an external plan, then activate the task graph."""

    state = request.app.state
    db = state.db
    queue = state.task_queue
    settings = state.settings

    # 1. Capability gate inputs: declared profile + (best-effort) run history.
    profile = await state.effective_settings.capability_profile(
        project_id=None, model=body.model
    )
    per_leaf_budget = int(profile.context_window * _LEAF_BUDGET_FRACTION)
    # History learning is a follow-up; with no rows the summarizer returns a
    # sentinel and the brain relies on the declared profile.
    history = summarize_outcomes([])
    prompt = build_review_prompt(body.plan, profile, history, per_leaf_budget)

    # 2. Brain-driven decomposition (must never pass silently on bad output).
    try:
        raw = await state.llm_router.run("plan_review", prompt, project_id=None)
        opus_plan = parse_review_response(raw)
    except PlanReviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"plan review failed: {exc}",
        ) from exc

    # 3. Bridge brain ids -> the slug convention TaskQueue requires.
    _normalize_slugs(opus_plan)

    # 4. Thread any curated, scrubbed context onto every leaf (mirror dispatch).
    scrubbed_context = scrub_context(body.context)
    if scrubbed_context is not None:
        for task in opus_plan["tasks"]:
            task.setdefault("context_text", scrubbed_context)

    # 5. Create + activate the plan via the existing TaskQueue path.
    harness = body.harness or default_harness_id()
    project_id = await _create_or_reuse_project(
        db, body.repo_url, None, body.model, harness
    )
    plan_id = await queue.create_plan(project_id, source="execute-plan")
    branch_name = body.branch or f"plan/execute-{_slugify(body.plan)}"
    await queue.activate_plan(plan_id, opus_plan, branch_name)

    leaves = [t["id"] for t in opus_plan["tasks"] if not t.get("needs_stronger_model")]
    blocked = [t["id"] for t in opus_plan["tasks"] if t.get("needs_stronger_model")]
    base_url = f"http://localhost:{getattr(settings, 'port', 8080)}/"
    return {
        "plan_id": plan_id,
        "project_id": project_id,
        "dashboard_url": base_url,
        "leaves": leaves,
        "blocked": blocked,
    }
