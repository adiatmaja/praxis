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
from orchestrator.models.schemas import DispatchRequest, DispatchResponse


router = APIRouter(tags=["dispatch"], dependencies=[Depends(verify_token)])

# Tokens that indicate no real GitHub token is configured.
_PLACEHOLDER_TOKENS: frozenset[str] = frozenset({"placeholder", ""})


def _slugify(text: str) -> str:
    """Build a short branch-safe slug from free text plus a uniqueness suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"
    return f"{base}-{uuid.uuid4().hex[:6]}"


async def _guard_base_sha(body: DispatchRequest, settings: Any, branch: str) -> None:
    """Reject the dispatch if ``expected_base_sha`` != current origin head.

    Read-only remote compare (``git ls-remote``). Guards against dispatching a
    worker against stale origin code when local commits were never pushed.

    Raises:
        HTTPException: 409 on mismatch, 502 on remote-communication failure.
    """
    provider = build_credential_provider(settings)
    git = GitOps(provider)
    try:
        origin_sha = await git.remote_head_sha(body.repo_url, branch)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not resolve origin base sha: {exc}",
        ) from exc
    if origin_sha is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"branch '{branch}' not found on remote for base-sha check",
        )
    expected = body.expected_base_sha or ""
    if not (origin_sha.startswith(expected) or expected.startswith(origin_sha)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"expected base sha '{expected}' does not match "
                f"origin/{branch} ('{origin_sha}'). Push your local commits "
                "or refetch origin, then retry."
            ),
        )


async def _preflight(body: DispatchRequest, settings: Any) -> list[str]:
    """Validate remote state before writing any DB rows.

    Args:
        body: The incoming dispatch request.
        settings: Application settings (must expose ``github_token``).

    Returns:
        A (possibly empty) list of non-fatal warning strings.

    Raises:
        HTTPException: On validation failure or upstream communication error.
    """
    # No branch and no plan_path: nothing to validate (fresh-branch flow).
    if body.branch is None and body.plan_path is None:
        if body.expected_base_sha is not None:
            await _guard_base_sha(body, settings, "main")
        return []

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

    # At this point branch is set (plan_path may or may not be).
    # Narrow the type: we have already handled the None case above.
    branch: str = body.branch  # type: ignore[assignment]
    github_token = getattr(settings, "github_token", "") or ""
    has_app = bool(
        getattr(settings, "github_app_id", None)
        and getattr(settings, "github_app_private_key", None)
    )
    if has_app or github_token:
        provider: GitHubCredentialProvider = build_credential_provider(settings)
    else:
        provider = PatCredentialProvider("")
    git = GitOps(provider)

    # Verify the branch exists on the remote.
    try:
        branch_exists = await git.remote_branch_exists(body.repo_url, branch)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not verify branch on remote: {exc}",
        ) from exc

    if not branch_exists:
        if body.plan_path is not None:
            detail = (
                f"branch '{branch}' was not found on the remote. "
                "Praxis reads only from GitHub: push the plan branch first, "
                "then retry."
            )
        else:
            detail = (
                f"branch '{branch}' was not found on the remote. "
                "Push it first, or omit 'branch' to let Praxis create a fresh "
                "branch from the default branch."
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        )

    warnings: list[str] = []

    if body.plan_path is not None:
        repo_slug = GitOps.repo_slug(body.repo_url)
        if repo_slug is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "plan_path validation is only supported for github.com repositories. "
                    "Supply a github.com repo_url or omit plan_path."
                ),
            )

        token_lower = github_token.lower().strip()
        if not has_app and token_lower in _PLACEHOLDER_TOKENS:
            warnings.append(
                "plan_path existence check skipped: no GitHub token is configured. "
                "Ensure the file exists on the remote branch before dispatching."
            )
        else:
            try:
                file_exists = await git.remote_file_exists(
                    repo_slug, branch, body.plan_path
                )
            except RuntimeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"could not verify plan_path on remote: {exc}",
                ) from exc

            if not file_exists:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"plan_path '{body.plan_path}' was not found on branch "
                        f"'{branch}' in '{repo_slug}'. "
                        "Praxis reads only from GitHub: push the file first, then retry."
                    ),
                )

    if body.expected_base_sha is not None:
        await _guard_base_sha(body, settings, branch)

    return warnings


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

    opus_plan = {"tasks": [task_dict]}
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
        "warnings": warnings,
    }
