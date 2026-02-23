"""Queue overview and management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from bugalizer.auth import require_api_key
from bugalizer.db import queue_counts, report_get, reset_triage_retries
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


@router.post("/queue/{report_id}/retry")
def retry_report(
    report_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, str]:
    """Reset retry count for a report, making it eligible for re-processing.

    Deletes failed triage analyses so the worker will pick it up again.
    """
    report = report_get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Bug report not found")
    if report["status"] != "triaged":
        raise HTTPException(
            status_code=409,
            detail=f"Report must be in 'triaged' status to retry (current: '{report['status']}')",
        )
    reset_triage_retries(report_id)
    return {"status": "ok", "message": f"Retry count reset for report {report_id}"}
