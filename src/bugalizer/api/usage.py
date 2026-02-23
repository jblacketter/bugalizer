"""Token usage endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from typing import Optional

from bugalizer.auth import require_api_key
from bugalizer.db import token_usage_summary
from bugalizer.models import UsageSummary

router = APIRouter(tags=["usage"])


@router.get("/usage")
def get_usage(
    _key: str = Depends(require_api_key),
) -> UsageSummary:
    """Get aggregate token usage across all projects."""
    data = token_usage_summary()
    return UsageSummary(**data)


@router.get("/usage/{project_id}")
def get_project_usage(
    project_id: str,
    _key: str = Depends(require_api_key),
) -> UsageSummary:
    """Get token usage for a specific project."""
    data = token_usage_summary(project_id=project_id)
    return UsageSummary(**data)
