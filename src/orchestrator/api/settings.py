"""Global settings overrides API."""

from __future__ import annotations

import json
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from orchestrator.api.auth import verify_token
from orchestrator.core.effective_settings import EDITABLE_KEYS, EffectiveSettings
from orchestrator.core.llm_router import CALL_SITE_DEFAULTS


router = APIRouter(tags=["settings"], dependencies=[Depends(verify_token)])


@router.get("/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    """Return editable settings (with override status) and read-only system info."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    settings = request.app.state.settings
    editable_raw: dict[str, Any] = await es.all_editable()
    return {
        "editable": editable_raw,
        "readonly": {
            "host": settings.host,
            "port": settings.port,
            "database_url": settings.database_url,
        },
    }


@router.put("/settings")
async def update_settings(
    request: Request,
    body: dict[str, str | None],
) -> dict[str, Any]:
    """Set or reset global settings overrides.

    Body: JSON object where keys are editable setting names and values are
    strings (to override) or null (to reset to env default).
    """
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    unknown = set(body) - EDITABLE_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown or non-editable keys: {sorted(unknown)}",
        )
    for key, value in body.items():
        await es.set_override(key, value)
    result: dict[str, Any] = await es.all_editable()
    return result


class ModelPut(BaseModel):
    call_site: str
    config: dict[str, Any]


class ModelReset(BaseModel):
    call_site: str | None = None


@router.get("/settings/models")
async def get_models(request: Request) -> dict[str, Any]:
    """Return all call-site configs merged with any DB overrides."""
    db = request.app.state.db
    resolved: dict[str, Any] = {}
    for site, default in CALL_SITE_DEFAULTS.items():
        row = await db.fetch_one(
            "SELECT value FROM settings_overrides WHERE key = ?",
            (f"models.{site}",),
        )
        cfg = dict(default)
        if row and row["value"]:
            cfg.update(json.loads(row["value"]))
        resolved[site] = {**cfg, "default": default}
    return resolved


@router.put("/settings/models")
async def put_models(request: Request, body: ModelPut) -> dict[str, str]:
    """Override a single call-site config."""
    if body.call_site not in CALL_SITE_DEFAULTS:
        raise HTTPException(status_code=400, detail="unknown call_site")
    await request.app.state.db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"models.{body.call_site}", json.dumps(body.config)),
    )
    return {"status": "ok"}


@router.post("/settings/models/reset")
async def reset_models(request: Request, body: ModelReset) -> dict[str, str]:
    """Reset one call-site override (or all if call_site is null)."""
    db = request.app.state.db
    if body.call_site:
        await db.execute(
            "DELETE FROM settings_overrides WHERE key = ?",
            (f"models.{body.call_site}",),
        )
    else:
        await db.execute("DELETE FROM settings_overrides WHERE key LIKE 'models.%'")
    return {"status": "ok"}
