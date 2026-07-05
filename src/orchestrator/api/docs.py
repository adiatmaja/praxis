"""Docs index API: list, raw content, refresh."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestrator.api.auth import verify_token
from orchestrator.models.schemas import DocResponse


router = APIRouter(prefix="/api/docs", tags=["docs"])


@router.get("", response_model=list[DocResponse])
async def list_docs(
    request: Request,
    category: str | None = None,
    _: None = Depends(verify_token),
) -> list[dict[str, Any]]:
    """List all indexed docs, optionally filtered by category."""
    db = request.app.state.db
    if category:
        return cast(
            list[dict[str, Any]],
            await db.fetch_all(
                "SELECT * FROM doc_index WHERE category = ? ORDER BY updated_at DESC",
                (category,),
            ),
        )
    return cast(
        list[dict[str, Any]],
        await db.fetch_all("SELECT * FROM doc_index ORDER BY updated_at DESC"),
    )


@router.post("/refresh")
async def refresh_docs(
    request: Request,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Trigger a full rescan of the docs root."""
    return cast(dict[str, Any], await request.app.state.doc_indexer.scan())


@router.get("/raw")
async def raw_doc(
    request: Request,
    path: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Return raw markdown content for a doc by relative path."""
    if not re.match(r"^(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+\.[a-zA-Z0-9_.-]+$", path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path format"
        )
    db = request.app.state.db
    row = await db.fetch_one("SELECT path FROM doc_index WHERE path = ?", (path,))
    if not row:
        raise HTTPException(status_code=404, detail="doc not found")
    safe_path = row["path"]

    settings = request.app.state.settings
    root = Path(settings.docs_root).parent.resolve()
    target = (root / safe_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="doc not found")
    return {"path": path, "content": target.read_text(encoding="utf-8")}
