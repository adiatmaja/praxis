"""Harness catalog REST endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from orchestrator.api.auth import verify_token
from orchestrator.core.harnesses import list_harnesses


router = APIRouter(tags=["harnesses"], dependencies=[Depends(verify_token)])


@router.get("/harnesses")
async def get_harnesses() -> list[dict[str, Any]]:
    """Return the harness catalog (recommended first) for the UI About panel."""

    return list_harnesses()
