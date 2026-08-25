"""Plan REST endpoints."""

from __future__ import annotations

import logging
import subprocess  # noqa: S404
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token
from orchestrator.api.repo_errors import repo_failure_detail
from orchestrator.core.github_credentials import (
    CredentialError,
    build_credential_provider,
)
from orchestrator.core.markdown_utils import extract_frontmatter_field
from orchestrator.core.plan_derive import PlanDeriveError, derive_opus_plan
from orchestrator.core.spec_docs import render_spec_doc, spec_doc_path
from orchestrator.core.status_vocab import SATISFIED_STATUSES
from orchestrator.models.schemas import (
    PlanCreate,
    PlanResponse,
    PlanStatus,
    TaskStatus,
)


logger = logging.getLogger(__name__)
router = APIRouter(tags=["plans"], dependencies=[Depends(verify_token)])


@router.post(
    "/projects/{project_id}/plans",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanResponse,
)
async def create_plan(
    request: Request,
    project_id: str,
    body: PlanCreate,
) -> dict[str, Any]:
    """Create a pending plan for a project, persisting its spec as a doc.

    The submitted specification is committed to the repository as a spec doc
    and recorded on the plan as ``spec_path``; that doc is what the planner
    reads. Persisting it FIRST is deliberate: if the write fails there must be
    no plan, because a plan with no spec plans from the repository name alone
    and then dispatches real workers at a real repository.
    """

    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT id, repo_url FROM projects WHERE id = ?", (project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    spec_path = spec_doc_path(
        body.spec,
        today=datetime.now(UTC).date(),
        unique=uuid.uuid4().hex[:8],
    )
    # A missing GitHub credential must answer the SAME WAY here as it does from
    # POST /api/projects, which classifies it 422 with the remedy attached.
    # This route answered 502 Bad Gateway ("upstream is broken, retry") for a
    # permanent configuration fact that will never self-heal on a retry, so the
    # first two commands a newcomer types disagreed about the same install.
    try:
        build_credential_provider(request.app.state.settings)
    except CredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{exc}. Submitting a spec commits it to the repository as a "
                "doc, so it needs a GitHub credential."
            ),
        ) from exc

    try:
        await request.app.state.brainstorm.write_and_commit(
            project["repo_url"], spec_path, render_spec_doc(body.spec)
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, never plan spec-less
        logger.exception("Failed to persist submitted spec for project %s", project_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not persist the specification to the repository: {exc}",
        ) from exc

    plan_id = await request.app.state.task_queue.create_plan(
        project_id, spec_path=spec_path
    )
    plan = await request.app.state.task_queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=500, detail="Plan creation failed")
    return cast(dict[str, Any], plan)


@router.get("/projects/{project_id}/plans", response_model=list[PlanResponse])
async def list_plans(request: Request, project_id: str) -> list[dict[str, Any]]:
    """List plans for a project."""

    project = await request.app.state.db.fetch_one(
        "SELECT id FROM projects WHERE id = ?",
        (project_id,),
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return cast(
        list[dict[str, Any]],
        await request.app.state.task_queue.get_plans_for_project(project_id),
    )


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(request: Request, plan_id: str) -> dict[str, Any]:
    """Get a plan by ID."""

    plan = await request.app.state.task_queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    return cast(dict[str, Any], plan)


@router.post("/plans/{plan_id}/approve", response_model=PlanResponse)
async def approve_plan(request: Request, plan_id: str) -> dict[str, Any]:
    """Approve a pending/autonomous plan."""

    queue = request.app.state.task_queue
    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    await queue.update_plan_status(plan_id, PlanStatus.ACTIVE)
    updated = await queue.get_plan(plan_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Plan approval failed")
    return cast(dict[str, Any], updated)


@router.post("/plans/{plan_id}/reject", response_model=PlanResponse)
async def reject_plan(request: Request, plan_id: str) -> dict[str, Any]:
    """Reject a plan."""

    queue = request.app.state.task_queue
    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    await queue.update_plan_status(plan_id, PlanStatus.REJECTED)
    updated = await queue.get_plan(plan_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Plan rejection failed")
    return cast(dict[str, Any], updated)


class PromoteRequest(BaseModel):
    """Request body for the promote-to-run endpoint."""

    project_id: str
    plan_path: str


@router.post(
    "/plans/promote",
    status_code=status.HTTP_201_CREATED,
    response_model=PlanResponse,
)
async def promote_plan(request: Request, body: PromoteRequest) -> dict[str, Any]:
    """Derive tasks from a plan.md and create + activate a runnable plan."""

    db = request.app.state.db
    queue = request.app.state.task_queue
    project = await db.fetch_one(
        "SELECT repo_url, lm_studio_url FROM projects WHERE id = ?",
        (body.project_id,),
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    # Idempotency: reuse an existing run for this plan_path.
    existing = await db.fetch_one(
        "SELECT * FROM plans WHERE project_id = ? AND plan_path = ?",
        (body.project_id, body.plan_path),
    )
    if existing is not None:
        return cast(dict[str, Any], existing)

    try:
        plan_md = await request.app.state.brainstorm.read_doc(
            project["repo_url"], body.plan_path
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except subprocess.CalledProcessError as exc:
        # Same family, same handler: `str()` on this is only the exit code and
        # the reason lives on `.stderr`.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=repo_failure_detail(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"repo read failed: {exc}"
        ) from exc

    try:
        opus_plan = await derive_opus_plan(
            plan_md, lm_studio_url=project["lm_studio_url"] or ""
        )
    except PlanDeriveError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"task derivation failed: {exc}",
        ) from exc

    spec_path = extract_frontmatter_field(plan_md, "spec_path")
    plan_id = await queue.create_plan(
        body.project_id, opus_plan["plan_summary"], source="promoted"
    )
    await db.execute(
        "UPDATE plans SET spec_path = ?, plan_path = ? WHERE id = ?",
        (spec_path, body.plan_path, plan_id),
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    branch = f"plan/{today}-{opus_plan['plan_slug']}"
    await queue.activate_plan(plan_id, opus_plan, branch)

    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=500, detail="promotion failed")
    return cast(dict[str, Any], plan)


@router.post("/plans/{plan_id}/approve-merges")
async def approve_merges(request: Request, plan_id: str) -> dict[str, Any]:
    """Approve and merge every review-passed task in a plan, then integrate it.

    A plan reaches the gate in two stages and this verb has to cover both, or
    it reports success while the work sits somewhere the operator did not ask
    for it:

    1. Each review-passed task merges onto the plan branch.
    2. When nothing is left parked, the plan's own integration PR merges onto
       the project's base branch.

    Running this against a finished plan used to return ``approved: 0`` and
    exit clean while an integration PR sat open, which is why the response now
    carries an explicit ``integration`` block: the caller can tell "there was
    nothing to do" apart from "I did the last step".
    """
    queue = request.app.state.task_queue
    db = request.app.state.db
    orchestrator = request.app.state.orchestrator
    plan = await queue.get_plan(plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
        )
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator unavailable",
        )
    project = await db.fetch_one(
        "SELECT * FROM projects WHERE id = ?", (plan["project_id"],)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    tasks = await queue.get_tasks_for_plan(plan_id)
    approved = 0
    errors: list[dict[str, str]] = []
    for task in tasks:
        if task["status"] != TaskStatus.PASSED:
            continue
        # Re-read, because a task can leave the gate DURING this loop. In
        # single-branch mode every task in the plan shares ONE pull request,
        # and merging it lands all of them, so `approve_task_merge` sweeps the
        # siblings that same merge landed. The snapshot above still calls them
        # PASSED; approving one now raises "not awaiting merge", and one
        # collected error suppresses `_integrate_plan` for the whole plan, so
        # the verb reports failures for a plan it just merged completely.
        current = await queue.get_task(task["id"])
        if current is None:
            continue
        if current["status"] == TaskStatus.MERGED:
            # Only a task this request found PASSED reaches here, so MERGED
            # now means this request merged it. Counting it keeps `approved`
            # the number of tasks that left the gate rather than the number of
            # merge calls made, which on a shared PR is 1 for any N.
            approved += 1
            continue
        if current["status"] != TaskStatus.PASSED:
            continue
        try:
            await orchestrator.approve_task_merge(task["id"], project)
            approved += 1
        except ValueError as exc:
            msg = exc.args[0] if exc.args else "Validation error"
            errors.append({"task_id": task["id"], "error": msg})
        except Exception:  # noqa: BLE001 - collect, keep going
            logger.exception("Failed to approve task merge for task %s", task["id"])
            errors.append(
                {
                    "task_id": task["id"],
                    "error": "Failed to approve task merge due to an internal error",
                }
            )

    integration = await _integrate_plan(orchestrator, queue, plan_id, project, errors)
    return {
        "plan_id": plan_id,
        "approved": approved,
        "integration": integration,
        "errors": errors,
    }


async def _integrate_plan(
    orchestrator: Any,
    queue: Any,
    plan_id: str,
    project: dict[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """Merge the plan's integration PR when the plan is finished and clean.

    Returns a status block rather than a bare bool so the CLI can say which of
    the several "nothing happened" cases it is looking at. The three that are
    NOT failures:

    - ``not_ready``: tasks are still parked or in flight. Integrating now
      would carry a partial plan onto the base branch.
    - ``none``: the plan completed but no integration PR exists (best-effort
      creation failed, or the plan is not complete at all).
    - ``already_merged``: someone already integrated it.

    A merge that is attempted and fails appends to ``errors``, so the caller
    still exits non-zero. Task-merge errors earlier in the same request
    suppress the attempt entirely: those tasks are not on the plan branch, so
    integrating would land an incomplete plan.
    """
    if errors:
        return {"status": "not_ready", "reason": "task merges failed"}

    plan = await queue.get_plan(plan_id)
    if plan is None:
        return {"status": "none", "reason": "plan not found"}
    if plan.get("integration_merged_at"):
        return {
            "status": "already_merged",
            "pr_url": plan.get("integration_pr_url"),
        }
    if not plan.get("integration_pr_url"):
        return {"status": "none", "reason": "no integration PR for this plan"}

    # SATISFIED_STATUSES, not a literal pair: a NO_CHANGES leaf is finished
    # with (its work was already present) and a plan full of them would
    # otherwise never be integrable.
    remaining = [
        t
        for t in await queue.get_tasks_for_plan(plan_id)
        if t["status"] not in SATISFIED_STATUSES
    ]
    if remaining:
        return {
            "status": "not_ready",
            "reason": f"{len(remaining)} task(s) not merged yet",
        }

    try:
        pr_url = await orchestrator.approve_plan_integration(plan_id, project)
    except ValueError as exc:
        msg = exc.args[0] if exc.args else "Validation error"
        errors.append({"task_id": plan_id, "error": msg})
        return {"status": "failed", "reason": msg}
    except Exception:  # noqa: BLE001 - collect, mirror the task-merge path
        logger.exception("Failed to merge integration PR for plan %s", plan_id)
        errors.append(
            {
                "task_id": plan_id,
                "error": "Failed to merge the integration PR due to an internal error",
            }
        )
        return {"status": "failed", "reason": "internal error"}
    return {"status": "merged", "pr_url": pr_url}
