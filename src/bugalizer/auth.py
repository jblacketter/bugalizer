"""API key authentication middleware."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from bugalizer.config import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    api_key: str | None = Security(_api_key_header),
) -> str:
    """Dependency that enforces API key auth.

    If no API keys are configured (BUGALIZER_API_KEYS is empty), auth is
    disabled and all requests are allowed (returns "anonymous").
    """
    valid = settings.valid_api_keys()
    if not valid:
        return "anonymous"
    if not api_key or api_key not in valid:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return api_key
