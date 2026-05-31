"""Authentication dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orchestrator.config import Settings


security = HTTPBearer()


def get_settings() -> Settings:
    """Return current application settings from environment."""

    return Settings()


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate bearer token against configured auth token."""

    if credentials.credentials != settings.auth_token:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    return credentials.credentials
