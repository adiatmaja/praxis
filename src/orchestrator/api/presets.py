"""Worker presets API: named (harness, model, endpoint) triples."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

from fastapi import APIRouter, Depends, Request

from orchestrator.api.auth import verify_token
from orchestrator.core.effective_settings import EffectiveSettings


router = APIRouter(tags=["settings"], dependencies=[Depends(verify_token)])


@router.get("/settings/presets")
async def get_presets(request: Request) -> dict[str, Any]:
    """Return the shipped worker presets, in declaration order."""
    es = cast(EffectiveSettings, request.app.state.effective_settings)
    presets = await es.worker_presets()
    return {"presets": [asdict(p) for p in presets]}
