"""Global settings overrides API."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from orchestrator.api.auth import verify_token
from orchestrator.core.effective_settings import EDITABLE_KEYS, EffectiveSettings


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
