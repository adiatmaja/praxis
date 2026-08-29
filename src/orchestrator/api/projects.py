"""Project REST endpoints."""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

from orchestrator.api.auth import verify_token
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import (
    CredentialError,
    build_credential_provider,
)
from orchestrator.core.preflight import (
    PreflightError,
    assert_repo_url_allowed,
    credential_configured,
    preflight_remote,
    status_and_detail,
)
from orchestrator.models.schemas import ProjectCreate, ProjectResponse, ProjectUpdate


router = APIRouter(tags=["projects"], dependencies=[Depends(verify_token)])


@router.post(
    "/projects",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectResponse,
)
async def create_project(request: Request, body: ProjectCreate) -> dict[str, Any]:
    """Create a project for the first configured user."""

    db = request.app.state.db
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

    # A second project row for a repository this install already knows is
    # UNREACHABLE, so creating it and answering 201 Created is the server
    # reporting work it did not do. Every path that resolves a repository to a
    # project selects `WHERE repo_url = ? ORDER BY rowid LIMIT 1`
    # (`api/execute_plan.py`, `api/dispatch.py`), so the FIRST row wins
    # permanently: the new row is never dispatched against, never listed as the
    # answer to anything, and its `default_branch` - the field the second row
    # is usually created to change - is inert. The caller then watches plans
    # land against settings they thought they had replaced.
    #
    # 409, not a silent reuse: reusing would quietly apply the caller's
    # `model_name`, `harness` and `verify_cmd` to a project they did not name,
    # which is the same surprise wearing the opposite sign.
    #
    # The match is EXACT string equality, deliberately the same comparison the
    # resolvers make. A looser one here would refuse a URL that those queries
    # would treat as a different repository, which turns an honest 201 into an
    # unexplainable 409.
    existing = await db.fetch_one(
        "SELECT id, name, default_branch FROM projects WHERE repo_url = ? "
        "ORDER BY rowid LIMIT 1",
        (body.repo_url,),
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Project {existing['id']} ('{existing['name']}') already "
                f"tracks {body.repo_url} on base branch "
                f"'{existing['default_branch']}'. A repository resolves to its "
                "FIRST project row everywhere, so a second one would be "
                "created and then never used. Change the existing project with "
                "'praxis configure' (or PATCH /api/projects/"
                f"{existing['id']}), or delete it first if you meant to start "
                "over."
            ),
        )

    # NOTE: the protected-branch guard (main/master/release*) applies only to
    # WORK/BASE branches at execute_plan and dispatch, NOT here: a project's
    # configured default_branch legitimately IS the protected branch (main), so
    # rejecting it would be wrong. See core/merge_policy.is_protected_branch.
    # Preflight: validate remote reachability before inserting a project row.
    settings = request.app.state.settings
    resolved_model = body.model_name or settings.default_worker_model
    resolved_harness = body.harness or settings.default_worker_harness
    if not resolved_model:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="model_name is required and no default_worker_model is configured",
        )

    try:
        assert_repo_url_allowed(body.repo_url, settings)
    except PreflightError as exc:
        sc, detail = status_and_detail(exc)
        raise HTTPException(status_code=sc, detail=detail) from exc

    # A missing GitHub credential is the operator's configuration, not a server
    # fault, and `build_credential_provider` already raises carrying the exact
    # remedy. Uncaught, that became a bare `500 Internal Server Error` with the
    # remedy left in the container log: the CLI printed nine words that say the
    # SERVER is broken, for the single most likely first command on an install
    # set up without GitHub credentials. Found on the live install in
    # walkthrough #10, immediately after the doctor had reported "local mode:
    # no GitHub credential configured, which is correct for evaluating with a
    # file:// repo", which is true and is why this url is the thing that has to
    # explain itself.
    try:
        provider = build_credential_provider(settings)
    except CredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{exc}. This repo_url needs a GitHub credential; a local "
                "file:// path needs none, and allow_local_repo_paths in the "
                "settings file enables that mode."
            ),
        ) from exc
    git = GitOps(provider)
    try:
        await preflight_remote(
            git,
            body.repo_url,
            base=body.default_branch,
            credential_configured=credential_configured(settings),
        )
    except PreflightError as exc:
        sc, detail = status_and_detail(exc)
        raise HTTPException(status_code=sc, detail=detail) from exc

    project_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, default_branch, approval_gate, auto_merge,
            verify_cmd, confidence_threshold, max_retries, max_improvement_cycles,
            lm_studio_url, model_name, harness, agent_model, agent_model_effort,
            context_window)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            user["id"],
            body.name,
            body.repo_url,
            body.default_branch,
            body.approval_gate,
            body.auto_merge,
            body.verify_cmd,
            body.confidence_threshold,
            body.max_retries,
            body.max_improvement_cycles,
            body.lm_studio_url,
            resolved_model,
            resolved_harness,
            body.agent_model,
            body.agent_model_effort,
            body.context_window,
        ),
    )
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(status_code=500, detail="Project creation failed")
    return cast(dict[str, Any], project)


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List projects."""

    return cast(
        list[dict[str, Any]],
        await request.app.state.db.fetch_all(
            "SELECT * FROM projects ORDER BY created_at DESC, rowid DESC"
        ),
    )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(request: Request, project_id: str) -> dict[str, Any]:
    """Get a project by ID."""

    project = await request.app.state.db.fetch_one(
        "SELECT * FROM projects WHERE id = ?",
        (project_id,),
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return cast(dict[str, Any], project)


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    request: Request,
    project_id: str,
    body: ProjectUpdate,
) -> dict[str, Any]:
    """Update mutable project settings."""

    db = request.app.state.db
    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    updates = body.model_dump(exclude_none=True)
    if updates:
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        await db.execute(
            # Keys come from the Pydantic model's field names, never from a
            # caller; every value is bound via the params tuple below.
            f"UPDATE projects SET {set_clause} WHERE id = ?",  # noqa: S608  # nosec B608
            (*updates.values(), project_id),
        )

    updated = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if updated is None:
        raise HTTPException(status_code=500, detail="Project update failed")
    return cast(dict[str, Any], updated)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(request: Request, project_id: str) -> Response:
    """Delete a project and everything that references it.

    ``Database.initialize()`` runs with ``PRAGMA foreign_keys=ON``, so a bare
    ``DELETE FROM projects`` on a project with plans/tasks/agent_runs still
    attached raised ``sqlite3.IntegrityError`` straight into a bare 500 with
    no indication of what was still attached. A newcomer cleaning up a
    throwaway project hit this on the first non-empty one they tried to
    remove, with no other verb available to detach the children first.
    Cascading here (rather than refusing with a 409) is the option that
    leaves the operator with something to do next instead of a dead end.

    Deletes leaf-first (agent_runs, then tasks, then plans, then the project)
    so no intermediate step trips the same foreign key constraint.
    """

    db = request.app.state.db
    project = await db.fetch_one("SELECT id FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    await db.execute(
        """DELETE FROM agent_runs WHERE task_id IN (
               SELECT id FROM tasks WHERE plan_id IN (
                   SELECT id FROM plans WHERE project_id = ?
               )
           )""",
        (project_id,),
    )
    await db.execute(
        """DELETE FROM tasks WHERE plan_id IN (
               SELECT id FROM plans WHERE project_id = ?
           )""",
        (project_id,),
    )
    await db.execute("DELETE FROM plans WHERE project_id = ?", (project_id,))
    await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
