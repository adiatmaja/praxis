"""Global settings overrides API."""

from __future__ import annotations

import json
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from orchestrator.api.auth import verify_token
from orchestrator.core.capabilities import CapabilityCatalog
from orchestrator.core.effective_settings import EDITABLE_KEYS, EffectiveSettings
from orchestrator.core.llm_router import CALL_SITE_DEFAULTS
from orchestrator.core.roles import MODEL_ROLES
from orchestrator.models.schemas import RegisteredModel, RoleChains


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


@router.get("/settings/registry")
async def get_registry(request: Request) -> list[dict[str, Any]]:
    """Return the model registry (DB override or YAML default)."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    return await es.registered_models()


@router.put("/settings/registry")
async def put_registry(
    request: Request, body: list[RegisteredModel]
) -> list[dict[str, Any]]:
    """Replace the model registry."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    payload = [m.model_dump() for m in body]
    await es.set_override("models.registry", json.dumps(payload))
    return await es.registered_models()


@router.get("/settings/roles")
async def get_roles(request: Request) -> dict[str, list[str]]:
    """Return the per-role fallback chains."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    return await es.role_chains()


@router.put("/settings/roles")
async def put_roles(request: Request, body: RoleChains) -> dict[str, list[str]]:
    """Replace the per-role fallback chains (validated against the registry)."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    known = {m["name"] for m in await es.registered_models()}
    for role, chain in body.chains.items():
        if role not in MODEL_ROLES:
            raise HTTPException(422, detail=f"unknown role: {role}")
        if not chain:
            raise HTTPException(422, detail=f"role {role} chain must be non-empty")
        unknown = [n for n in chain if n not in known]
        if unknown:
            raise HTTPException(422, detail=f"unknown models in {role}: {unknown}")
    await es.set_override("models.roles", json.dumps(body.chains))
    return await es.role_chains()


@router.get("/settings/capabilities")
async def get_capabilities(request: Request) -> dict[str, Any]:
    """Return the bundled capability snapshot keyed by model id."""
    catalog = CapabilityCatalog()
    return {"as_of": catalog.as_of, "models": catalog.all()}


@router.post("/settings/capabilities/refresh")
async def refresh_capabilities() -> dict[str, str]:
    """Soft refresh stub — bundled snapshot only for v1 (offline-first)."""
    return {
        "status": "skipped",
        "detail": "Capability data is a bundled snapshot; live refresh not configured.",
    }


class AutoDelegatePut(BaseModel):
    enabled: bool


@router.get("/settings/auto-delegate")
async def get_auto_delegate(request: Request) -> dict[str, Any]:
    """Return auto-delegate mode state and the resolved default worker."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    return {
        "enabled": await es.auto_delegate_enabled(),
        "worker": es.auto_delegate_worker(),
    }


@router.put("/settings/auto-delegate")
async def put_auto_delegate(request: Request, body: AutoDelegatePut) -> dict[str, Any]:
    """Toggle auto-delegate mode on or off."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    await es.set_override("auto_delegate.enabled", "true" if body.enabled else None)
    return {
        "enabled": await es.auto_delegate_enabled(),
        "worker": es.auto_delegate_worker(),
    }
