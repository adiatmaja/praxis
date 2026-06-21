"""Aggregate spec docs, plan docs, and DB runs into lifecycle objects."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token


router = APIRouter(tags=["lifecycle"], dependencies=[Depends(verify_token)])


@router.get("/projects/{project_id}/lifecycle")
async def list_lifecycle(request: Request, project_id: str) -> list[dict[str, Any]]:
    """One entry per spec, joined to its plan doc and DB run."""
    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    try:
        docs = request.app.state.brainstorm.list_lifecycle_docs(project["repo_url"])
    except Exception as exc:  # noqa: BLE001 - surface clone/git failure as 502
        raise HTTPException(status_code=502, detail=f"repo read failed: {exc}") from exc

    specs = [d for d in docs if d["category"] == "spec"]
    plans_by_spec = {
        d["spec_path"]: d
        for d in docs
        if d["category"] == "plan" and d.get("spec_path")
    }

    db_plans = await request.app.state.task_queue.get_plans_for_project(project_id)
    runs_by_plan_path: dict[str, dict[str, Any]] = {}
    for p in db_plans:
        row = dict(p)
        plan_path = row.get("plan_path")
        if plan_path:
            runs_by_plan_path[plan_path] = row

    items: list[dict[str, Any]] = []
    for spec in specs:
        plan_doc = plans_by_spec.get(spec["path"])
        plan_path = plan_doc["path"] if plan_doc else None
        run = runs_by_plan_path.get(plan_path) if plan_path else None
        stage = "run" if run else "plan" if plan_doc else "spec"
        items.append(
            {
                "spec_path": spec["path"],
                "title": spec["title"],
                "plan_path": plan_path,
                "plan_progress": (
                    [plan_doc["done_count"], plan_doc["total_count"]]
                    if plan_doc
                    else None
                ),
                "run_id": run["id"] if run else None,
                "run_status": run["status"] if run else None,
                "stage": stage,
            }
        )
    return items


@router.get("/projects/{project_id}/doc-raw")
async def get_doc_raw(request: Request, project_id: str, path: str) -> dict[str, str]:
    """Return one spec/plan doc's raw markdown from the target repo."""
    db = request.app.state.db
    project = await db.fetch_one(
        "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        content = request.app.state.brainstorm.read_doc(project["repo_url"], path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - clone/git failure
        raise HTTPException(status_code=502, detail=f"repo read failed: {exc}") from exc
    return {"path": path, "content": content}
