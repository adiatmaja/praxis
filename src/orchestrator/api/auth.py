"""Authentication dependencies."""

from __future__ import annotations

import secrets
from functools import lru_cache

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orchestrator.config import Settings


security = HTTPBearer()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return current application settings from environment."""

    return Settings()


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate bearer token against configured auth token.

    Uses ``secrets.compare_digest`` to prevent timing-oracle attacks.

    Note: the ``token_hash`` column in the ``users`` table stores the raw
    token string for v1 single-token auth (not a real cryptographic hash).
    The column name is a legacy artifact; do not rely on it being hashed.
    """
    import hashlib

    provided = credentials.credentials.encode()
    expected = settings.auth_token.get_secret_value().encode()

    if settings.auth_token_hashed:
        provided_hash = hashlib.sha256(provided).hexdigest().encode()
        if not secrets.compare_digest(provided_hash, expected):
            raise HTTPException(status_code=401, detail="Invalid authentication token")
    else:
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid authentication token")
    return credentials.credentials
