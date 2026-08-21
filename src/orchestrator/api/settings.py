"""Global settings overrides API."""

from __future__ import annotations

import json
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from orchestrator.api.auth import verify_token
from orchestrator.core.capabilities import CapabilityCatalog
from orchestrator.core.effective_settings import EDITABLE_KEYS, EffectiveSettings
from orchestrator.core.llm_router import CALL_SITE_DEFAULTS
from orchestrator.core.roles import MODEL_ROLES, ROLE_OF_CALL_SITE
from orchestrator.models.schemas import RegisteredModel, RoleChains


router = APIRouter(tags=["settings"], dependencies=[Depends(verify_token)])


async def _settings_file(call: Any, what: str) -> Any:
    """Await a settings-file read, reporting a bad YAML as the operator's own.

    ``settings_file.load_yaml_settings`` raises
    ``ValueError("Invalid YAML in <path>: <parse error>")``, which names the
    file and the position. Four routes let that escape as a bare 500, so the
    message an operator needed most stayed in the container log and the
    dashboard told them the server was broken. The settings file (located by
    ``core.settings_file.config_file_path``, never by a literal here) is
    mounted and hand-edited by design, so a syntax error in it is an ordinary
    operator mistake rather than a bug in Praxis.
    """
    try:
        return await call
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


async def _shadowing_role(request: Request, call_site: str) -> str | None:
    """Return the role whose chain shadows ``call_site``, or None.

    ``EffectiveSettings.call_site_chain`` returns a role's chain whenever the
    call site has a role and that role declares one, and never consults the
    per-call-site override in that case. Both conditions are needed: a role
    with no declared chain does not shadow anything.
    """
    role = ROLE_OF_CALL_SITE.get(call_site)
    if not role:
        return None
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    try:
        chains = await es.role_chains()
    except ValueError:
        # A malformed settings file is reported by the read routes; here it
        # only means the question cannot be answered, and claiming "not
        # shadowed" would be the same false reassurance being fixed.
        return None
    return role if chains.get(role) else None


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
    """Override a single call-site config.

    Reports whether the override can actually take EFFECT. A call site
    that has a role, and whose role declares a chain in the settings
    file, is resolved by ``EffectiveSettings.call_site_chain`` from that
    chain, which never consults this override at all. On a stock install
    that covers most call sites, so a bare ``{"status": "ok"}`` told the
    operator their change had landed while the loop went on running the
    role chain, with nothing anywhere recording the disagreement.
    """
    if body.call_site not in CALL_SITE_DEFAULTS:
        raise HTTPException(status_code=400, detail="unknown call_site")
    await request.app.state.db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"models.{body.call_site}", json.dumps(body.config)),
    )
    shadow = await _shadowing_role(request, body.call_site)
    if shadow:
        return {
            "status": "stored_but_shadowed",
            "shadowed_by_role": shadow,
            "detail": (
                f"Saved, but it will NOT take effect: the {shadow!r} role "
                "declares a chain in the settings file, and a role chain "
                "replaces the per-call-site config entirely. Clear the "
                "role chain first."
            ),
        }
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
    return cast(
        list[dict[str, Any]],
        await _settings_file(es.registered_models(), "model registry"),
    )


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
    return cast(
        dict[str, list[str]], await _settings_file(es.role_chains(), "role chains")
    )


@router.put("/settings/roles")
async def put_roles(request: Request, body: RoleChains) -> dict[str, list[str]]:
    """Replace the per-role fallback chains (validated against the registry)."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    known = {
        m["name"]
        for m in await _settings_file(es.registered_models(), "model registry")
    }
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
