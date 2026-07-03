"""Queue overview and management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from bugalizer.auth import require_api_key
from bugalizer.db import (
    queue_counts,
    report_failure_info,
    report_get,
    report_list,
    reset_stage_retries,
)
from bugalizer.models import FailedReport, QueueOverview

router = APIRouter(tags=["queue"])


@router.get("/queue")
def get_queue_overview(
    project_id: Optional[str] = Query(None),
    _key: str = Depends(require_api_key),
) -> QueueOverview:
    """Get bug report queue overview with counts by status and failed reports."""
    counts = queue_counts(project_id=project_id)
    total = sum(counts.values())

    # Surface reports parked with an unresolved pipeline failure (retry gate).
    failed: list[FailedReport] = []
    for report in report_list(project_id=project_id):
        info = report_failure_info(report["id"])
        if info:
            failed.append(
                FailedReport(
                    id=report["id"],
                    title=report["title"],
                    failed_stage=info["failed_stage"],
                    last_error=info["last_error"],
                    permanent=info["permanent"],
                )
            )
    return QueueOverview(total=total, by_status=counts, failed=failed)


@router.post("/queue/{report_id}/retry")
def retry_report(
    report_id: str,
    _key: str = Depends(require_api_key),
) -> dict[str, str]:
    """Reset retry counts for a report, making it eligible for re-processing.

    Deletes failed triage, localization, and fix analyses so the worker's
    eligibility queries (which derive retry-exhaustion from those rows) will
    dispatch the report again.
    """
    report = report_get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Bug report not found")
    if report["status"] != "triaged":
        raise HTTPException(
            status_code=409,
            detail=f"Report must be in 'triaged' status to retry (current: '{report['status']}')",
        )
    reset_stage_retries(report_id)
    return {"status": "ok", "message": f"Retry counts reset for report {report_id}"}
