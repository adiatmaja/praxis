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
from orchestrator.core.git_backend import is_local_repo_url
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import (
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
from orchestrator.models.schemas import (
    ExecutePlanRequest,
    ExecutePlanResponse,
    PlanStatus,
)


router = APIRouter(tags=["execute-plan"], dependencies=[Depends(verify_token)])


# Keep _normalize_slugs as a re-export so existing tests that import it by the
# old private name continue to work.
_normalize_slugs = normalize_slugs
__all__ = ["_normalize_slugs"]


async def _create_or_reuse_project(
    db: Any,
    repo_url: str,
    name: str | None,
    model: str,
    harness: str | None,
    default_worker: dict[str, str] | None = None,
) -> str:
    """Return an existing project id for the repo, or create one. Mirrors dispatch.

    A None ``harness`` means "the caller expressed no preference". For an
    existing project that preserves whatever it was configured with; only a new
    project falls back to the registry default. Defaulting eagerly used to
    re-point an agy project at opencode on every plan submitted without the
    field, which made "which harness ran this" unanswerable.

    ``default_worker`` is the deployment's configured default worker (see
    ``EffectiveSettings.auto_delegate_worker()``), only consulted for a NEW
    project: an omitted harness on a brand-new project should get whatever
    worker this install is actually set up to run, the same way
    ``POST /api/projects`` already does, rather than always landing on
    ``default_harness_id()``'s hardcoded "opencode". It is optional and
    defaults to None so existing callers that pre-date this parameter keep
    their old behavior (registry default) unchanged.
    """
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
        (repo_url,),
    )
    if project is not None:
        project_id = project["id"]
        effective_harness = harness or project["harness"] or default_harness_id()
        await db.execute(
            "UPDATE projects SET model_name = ?, harness = ? WHERE id = ?",
            (model, effective_harness, project_id),
        )
        return str(project_id)

    # A default worker is only "configured" (as opposed to merely present as
    # a field default) when its model is set — the same convention
    # api/projects.py already uses ("model_name is required and no
    # default_worker_model is configured"). Without this check,
    # default_worker_harness's own field default ("opencode") would be
    # indistinguishable from default_harness_id(), silently masking whether
    # a worker was ever actually configured.
    new_project_harness = harness
    if new_project_harness is None and default_worker and default_worker.get("model"):
        new_project_harness = default_worker.get("harness") or None
    new_project_harness = new_project_harness or default_harness_id()

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
            new_project_harness,
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

    Returns immediately with the plan's stored status (``pending``) and a
    ``plan_id``. The orchestration loop picks the pending plan up on a later
    tick, runs the brain decomposition, and activates the task graph; watch
    ``dashboard_url``, or poll the plan, to track progress.

    The status is the one written to the row on purpose. It used to be
    ``"decomposing"``, which said the loop had started work it had not started
    (decomposition begins on a later tick, and only while the loop is running)
    and is not a member of the plan vocabulary, so a caller polling for it
    would never see that word again from any other surface.
    """
    state = request.app.state
    db = state.db
    queue = state.task_queue
    settings = state.settings

    # Guard: a local filesystem repo_url needs the deployment opt-in. Runs
    # unconditionally, unlike the expected_base_sha preflight below, because
    # this is the only layer that sees runtime settings.
    try:
        assert_repo_url_allowed(body.repo_url, settings)
    except PreflightError as exc:
        http_status, detail = status_and_detail(exc)
        raise HTTPException(status_code=http_status, detail=detail) from exc

    # Guard: a protected branch (main/master/release*) must never be used as a
    # plan base. Workers only ever target feature branches; targeting main would
    # collapse two-tier branching and let a worker PR aim at main directly. This
    # runs before any project/plan is created so it is a clean, side-effect-free
    # rejection.
    if body.branch and is_protected_branch(body.branch, "main"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"branch {body.branch} is a protected branch and cannot be used "
                "as a plan base; omit branch to auto-create "
                "plan/execute-<slug>, or pass a feature branch"
            ),
        )

    expected: str | None = None
    if body.expected_base_sha is not None:
        expected = (body.expected_base_sha or "").strip()
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expected_base_sha must not be empty",
            )

    # A LOCAL repo_url ALWAYS gets preflighted, not only when the caller
    # supplied a base sha. The opt-in above answers "may a local path be used
    # at all"; only `_preflight_local` answers "is this path a bare git repo",
    # and this is the one endpoint of the three where that check used to be
    # conditional. Without it `{"repo_url": "/"}` was accepted, a project row
    # was written, and `agent_manager.local_repo_volume` would bind-mount the
    # entire host filesystem read-write into an agent container.
    is_local = is_local_repo_url(body.repo_url)
    if expected is not None or is_local:
        base = body.branch or "main"
        cred_configured = credential_configured(settings)
        # Local mode has no credential by design, and building a GitHub
        # provider without one is not guaranteed to succeed; preflight
        # short-circuits on a local URL before it touches `git` anyway.
        provider: Any = (
            build_credential_provider(settings)
            if cred_configured
            else PatCredentialProvider("")
        )
        try:
            await preflight_remote(
                GitOps(provider),
                body.repo_url,
                base=base,
                expected_base_sha=expected,
                credential_configured=cred_configured,
            )
        except PreflightError as exc:
            raise HTTPException(
                status_code=status_and_detail(exc)[0],
                detail=status_and_detail(exc)[1],
            ) from exc

    project_id = await _create_or_reuse_project(
        db,
        body.repo_url,
        None,
        body.model,
        body.harness,
        default_worker=state.effective_settings.auto_delegate_worker(),
    )
    branch_name = body.branch or f"plan/execute-{branch_slug(body.plan)}"
    pending_input = json.dumps(
        {
            "plan": body.plan,
            "model": body.model,
            "context": body.context,
            "local_context": body.local_context,
            "branch": branch_name,
        }
    )
    plan_id = await queue.create_pending_execute_plan(project_id, pending_input)

    return {
        "plan_id": plan_id,
        "project_id": project_id,
        "dashboard_url": settings.dashboard_url(),
        # Read from the enum, not typed again: the value a caller polls for has
        # to be the value the row holds.
        "status": PlanStatus.PENDING.value,
    }
