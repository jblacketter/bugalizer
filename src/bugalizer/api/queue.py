"""Queue overview endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from typing import Optional

from bugalizer.auth import require_api_key
from bugalizer.db import queue_counts
from bugalizer.models import QueueOverview

router = APIRouter(tags=["queue"])


@router.get("/queue")
def get_queue_overview(
    project_id: Optional[str] = Query(None),
    _key: str = Depends(require_api_key),
) -> QueueOverview:
    """Get bug report queue overview with counts by status."""
    counts = queue_counts(project_id=project_id)
    total = sum(counts.values())
    return QueueOverview(total=total, by_status=counts)
