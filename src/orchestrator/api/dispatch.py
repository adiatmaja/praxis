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
from orchestrator.core.context_scrub import scrub_context
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
    build_credential_provider,
)
from orchestrator.core.harnesses import default_harness_id
from orchestrator.core.merge_policy import is_protected_branch
from orchestrator.core.preflight import (
    PreflightError,
    assert_repo_url_allowed,
    credential_configured,
    preflight_remote,
    status_and_detail,
)
from orchestrator.models.schemas import DispatchRequest, DispatchResponse


router = APIRouter(tags=["dispatch"], dependencies=[Depends(verify_token)])


def _slugify(text: str) -> str:
    """Build a short branch-safe slug from free text plus a uniqueness suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"
    return f"{base}-{uuid.uuid4().hex[:6]}"


async def _preflight(body: DispatchRequest, settings: Any) -> list[str]:
    """Validate remote state before writing any DB rows.

    Delegates read-only remote checks to the shared :func:`preflight_remote`
    and maps :class:`PreflightError` kinds to HTTP statuses.

    Args:
        body: The incoming dispatch request.
        settings: Application settings (must expose ``github_token``).

    Returns:
        A (possibly empty) list of non-fatal warning strings.

    Raises:
        HTTPException: On validation failure or upstream communication error.
    """
    # Guard: a local filesystem repo_url needs the deployment opt-in. The
    # schema admits the form; only here are runtime settings reachable.
    try:
        assert_repo_url_allowed(body.repo_url, settings)
    except PreflightError as exc:
        http_status, detail = status_and_detail(exc)
        raise HTTPException(status_code=http_status, detail=detail) from exc

    # Guard: a protected branch (main/master/release*) must never be used as a
    # base branch. Workers only target feature branches; a re-dispatch always
    # creates a fresh PR off the base. This runs before any DB writes.
    if body.branch and is_protected_branch(body.branch, "main"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"branch {body.branch} is a protected branch and cannot be used "
                "as a plan base; omit branch to auto-create plan/mcp-<slug>, "
                "or pass a feature branch"
            ),
        )

    # plan_path without branch: we cannot know which remote ref to check.
    if body.plan_path is not None and body.branch is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "plan_path requires branch: Praxis reads only from the GitHub remote, "
                "never from the caller's local workspace. "
                "Push the plan branch and supply its name via the 'branch' field."
            ),
        )

    # Empty expected_base_sha guard (preserved from original behavior).
    if body.expected_base_sha is not None:
        expected = body.expected_base_sha.strip()
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expected_base_sha must not be empty",
            )

    cred_configured = credential_configured(settings)

    if cred_configured:
        provider: GitHubCredentialProvider = build_credential_provider(settings)
    else:
        provider = PatCredentialProvider("")
    git = GitOps(provider)

    try:
        warnings = await preflight_remote(
            git,
            body.repo_url,
            base="main",
            branch=body.branch,
            plan_path=body.plan_path,
            expected_base_sha=body.expected_base_sha,
            credential_configured=cred_configured,
        )
    except PreflightError as exc:
        http_status, detail = status_and_detail(exc)
        raise HTTPException(
            status_code=http_status,
            detail=detail,
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"remote preflight failed: {exc}",
        ) from exc

    return warnings


async def _preflight_micro_edit(request: Request, body: DispatchRequest) -> None:
    """Reject a micro edit the lane cannot honestly run, at the boundary.

    Both conditions are checked again where the lane actually executes, because
    the mode can be toggled between this call and the dispatch. Checking here
    as well is what turns "the task failed" into "the request was refused, and
    here is why", which is the difference between a caller that can correct
    itself and one that has to read a task's feedback to find out.

    Args:
        request: The live request, for ``app.state.effective_settings``.
        body: The dispatch payload.

    Raises:
        HTTPException: 422 when no branch was named, or when auto-delegate is
            off.
    """
    if body.micro_edit is None:
        return
    if body.branch is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "micro_edit requires branch: the lane commits to the shared "
                "work branch you name, and bounds the review to the commit it "
                "makes there. Without a branch there is nothing to commit to "
                "and nothing to bound."
            ),
        )
    es = getattr(request.app.state, "effective_settings", None)
    enabled = False
    if es is not None:
        enabled = await es.auto_delegate_enabled()
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "micro_edit requires auto-delegate mode, which is off. v1 of "
                "the lane is scoped to the single shared work branch that mode "
                "creates; turn it on with `praxis mode on`, or dispatch this "
                "as an ordinary task."
            ),
        )


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

    # Preflight: validate remote state before touching the database.
    warnings = await _preflight(body, settings)
    await _preflight_micro_edit(request, body)

    user = await db.fetch_one("SELECT id FROM users LIMIT 1")
    if user is None:
        raise HTTPException(
            # 503, not 500. This is a recoverable state of the INSTALL, not
            # a bug in the request or the server, and the old wording named
            # an action ("seed a user") that no CLI verb or endpoint
            # performs, so an operator reading it had nothing to do.
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The database has no user row. It predates the automatic "
                "seeding added in a later version, so nothing will create "
                "one: stop the orchestrator, delete data/orchestrator.db, "
                "and restart to rebuild it."
            ),
        )

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
                body.harness or default_harness_id(),
            ),
        )
    else:
        # A None harness means "no preference": preserve whatever the project
        # was already configured with instead of silently re-pointing it at
        # the registry default (see execute_plan._create_or_reuse_project).
        project_id = project["id"]
        effective_harness = body.harness or project["harness"] or default_harness_id()
        await db.execute(
            "UPDATE projects SET model_name = ?, harness = ? WHERE id = ?",
            (body.model, effective_harness, project_id),
        )

    plan_id = await queue.create_plan(project_id, source="mcp")
    slug = _slugify(body.instructions)
    task_dict: dict[str, Any] = {
        "title": body.instructions[:80],
        "description": body.instructions,
        "slug": slug,
        "depends_on": [],
    }
    if body.plan_path is not None:
        task_dict["plan_path"] = body.plan_path
    if body.plan_text is not None:
        task_dict["plan_text"] = body.plan_text
    scrubbed_context = scrub_context(body.context)
    if scrubbed_context is not None:
        task_dict["context_text"] = scrubbed_context
    scrubbed_local = scrub_context(body.local_context)
    if scrubbed_local is not None:
        task_dict["repo_memory"] = scrubbed_local
    if body.files is not None:
        task_dict["files"] = body.files
    if body.verification is not None:
        task_dict["verification"] = body.verification
    if body.neighbor_contracts is not None:
        task_dict["neighbor_contracts"] = body.neighbor_contracts
    if body.micro_edit is not None:
        task_dict["micro_edit"] = body.micro_edit.model_dump()

    opus_plan = {"tasks": [task_dict]}
    branch_name = body.branch or f"plan/mcp-{slug}"
    await queue.activate_plan(plan_id, opus_plan, branch_name)

    tasks = await queue.get_tasks_for_plan(plan_id)
    if not tasks:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Task activation produced no task",
        )

    return {
        "task_id": tasks[0]["id"],
        "plan_id": plan_id,
        "project_id": project_id,
        "status": "queued",
        "dashboard_url": settings.dashboard_url(),
        "warnings": warnings,
    }
