"""Read-only origin-HEAD state for a project's base branch.

Visualization-only: powers the dashboard sidebar widget so an operator can see
what commit origin is at and notice when their local checkout is ahead. Never
mutates anything; degrades gracefully to available=False on remote errors.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import (
    CredentialError,
    build_credential_provider,
)
from orchestrator.models.schemas import GitStateResponse


logger = logging.getLogger(__name__)

router = APIRouter(tags=["git-state"], dependencies=[Depends(verify_token)])


@router.get("/projects/{project_id}/git-state", response_model=GitStateResponse)
async def get_git_state(request: Request, project_id: str) -> GitStateResponse:
    """Return origin HEAD (sha + commit meta) for the project's base branch."""
    db = request.app.state.db
    settings = request.app.state.settings

    project = await db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )

    base = project["default_branch"] or "main"
    repo_url = project["repo_url"]
    # "No credential" is a state this endpoint already knows how to report: it
    # is one more reason the git state is unavailable, and it has a reason
    # string ready. Uncaught, it was a 500 on every poll of a credential-less
    # install, which is the shipped local-evaluation mode, and the dashboard
    # polls this. The RuntimeError path four lines down exists for exactly this
    # kind of thing; this one just was not routed into it.
    try:
        git = GitOps(build_credential_provider(settings))
    except CredentialError as exc:
        return GitStateResponse(base=base, available=False, detail=str(exc))

    try:
        sha = await git.remote_head_sha(repo_url, base)
    except RuntimeError as exc:
        safe_project_id = str(project_id).replace("\r", "").replace("\n", "")
        safe_exc = str(exc).replace("\r", "").replace("\n", "")
        logger.info("git-state unavailable for %s: %s", safe_project_id, safe_exc)
        return GitStateResponse(
            base=base, available=False, detail=str(exc.args[0] if exc.args else exc)
        )

    if sha is None:
        return GitStateResponse(
            base=base, available=False, detail=f"branch '{base}' not found on origin"
        )

    subject: str | None = None
    committed_at: str | None = None
    slug = GitOps.repo_slug(repo_url)
    if slug is not None:
        try:
            meta: dict[str, Any] = await git.remote_commit_meta(slug, sha)
            subject = meta.get("subject")
            committed_at = meta.get("committed_at")
        except RuntimeError as exc:
            safe_project_id = str(project_id).replace("\r", "").replace("\n", "")
            safe_exc = str(exc).replace("\r", "").replace("\n", "")
            logger.info(
                "git-state commit meta unavailable for %s: %s",
                safe_project_id,
                safe_exc,
            )

    return GitStateResponse(
        base=base,
        sha=sha,
        short_sha=sha[:7],
        subject=subject,
        committed_at=committed_at,
        available=True,
    )
