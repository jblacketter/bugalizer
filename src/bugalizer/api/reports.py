"""Bug report CRUD endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from bugalizer.auth import require_api_key
from bugalizer.db import (
    analyses_for_report,
    project_exists,
    report_create,
    report_delete,
    report_get,
    report_list,
    report_update_status,
)
from bugalizer.models import (
    BugReportCreate,
    BugReportListResponse,
    BugReportResponse,
    BugStatus,
    LocalizationResponse,
    StatusUpdateRequest,
    StatusUpdateResponse,
    TERMINAL_STATUSES,
    validate_transition,
)

router = APIRouter(tags=["reports"])


def _build_warnings(body: BugReportCreate) -> list[str]:
    """Return warnings for missing recommended fields."""
    warnings: list[str] = []
    if not body.steps_to_reproduce:
        warnings.append(
            "Missing 'steps_to_reproduce': Adding reproduction steps helps AI analyze the bug faster."
        )
    if not body.expected_behavior:
        warnings.append(
            "Missing 'expected_behavior': Describing what you expected helps identify the root cause."
        )
    if not body.actual_behavior:
        warnings.append(
            "Missing 'actual_behavior': Describing what actually happened clarifies the bug impact."
        )
    return warnings


def _row_to_response(row: dict, warnings: list[str] | None = None) -> BugReportResponse:
    return BugReportResponse(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        description=row["description"],
        steps_to_reproduce=row.get("steps_to_reproduce"),
        expected_behavior=row.get("expected_behavior"),
        actual_behavior=row.get("actual_behavior"),
        reporter=row["reporter"],
        url=row.get("url"),
        feature_area=row.get("feature_area"),
        severity=row.get("severity", "medium"),
        environment=row.get("environment"),
        labels=row.get("labels"),
        status=row["status"],
        resolution_reason=row.get("resolution_reason"),
        assigned_to=row.get("assigned_to"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        warnings=warnings or [],
    )


@router.post("/reports", status_code=201)
def create_report(
    body: BugReportCreate,
    _key: str = Depends(require_api_key),
) -> BugReportResponse:
    """Submit a new bug report.

    Hard required: title, description, reporter, project_id (422 if missing).
    Recommended: steps_to_reproduce, expected_behavior, actual_behavior
    (accepted but response includes warnings).
    """
    if not project_exists(body.project_id):
        raise HTTPException(status_code=404, detail=f"Project '{body.project_id}' not found")

    warnings = _build_warnings(body)

    row = report_create(
        project_id=body.project_id,
        title=body.title,
        description=body.description,
        reporter=body.reporter,
        steps_to_reproduce=body.steps_to_reproduce,
        expected_behavior=body.expected_behavior,
        actual_behavior=body.actual_behavior,
        url=body.url,
        feature_area=body.feature_area,
        severity=body.severity.value,
        environment=body.environment,
        labels=body.labels,
    )

    return _row_to_response(row, warnings)


@router.get("/reports")
def list_reports(
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    _key: str = Depends(require_api_key),
) -> BugReportListResponse:
    """List bug reports, optionally filtered by project_id and/or status."""
    rows = report_list(project_id=project_id, status=status)
    return BugReportListResponse(
        reports=[_row_to_response(r) for r in rows],
        total=len(rows),
    )


@router.get("/reports/{report_id}")
def get_report(
    report_id: str,
    _key: str = Depends(require_api_key),
) -> BugReportResponse:
    """Get a single bug report by ID."""
    row = report_get(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Bug report not found")
    return _row_to_response(row)


@router.patch("/reports/{report_id}/status")
def update_report_status(
    report_id: str,
    body: StatusUpdateRequest,
    _key: str = Depends(require_api_key),
) -> StatusUpdateResponse:
    """Update bug report status. Validates transition rules."""
    row = report_get(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Bug report not found")

    current = BugStatus(row["status"])
    target = body.status

    if current in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot transition from terminal status '{current.value}'",
        )

    if not validate_transition(current, target, enforce_phase_gating=True):
        raise HTTPException(
            status_code=409,
            detail=f"Invalid transition: '{current.value}' → '{target.value}'",
        )

    if target in TERMINAL_STATUSES and not body.resolution_reason:
        # For terminal states, resolution_reason is recommended but not required.
        pass

    updated = report_update_status(
        report_id, target.value, resolution_reason=body.resolution_reason
    )
    return StatusUpdateResponse(
        id=report_id,
        previous_status=current.value,
        new_status=target.value,
        resolution_reason=body.resolution_reason,
    )


@router.get("/reports/{report_id}/localization")
def get_localization(
    report_id: str,
    _key: str = Depends(require_api_key),
) -> LocalizationResponse:
    """Get localization results for a report."""
    row = report_get(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Bug report not found")

    analyses = analyses_for_report(report_id, phase="localization")
    if not analyses:
        raise HTTPException(status_code=404, detail="No localization results found")

    # Return the most recent localization
    latest = analyses[0]
    result = latest.get("result") or {}

    pass1 = result.get("pass1", {})
    pass2 = result.get("pass2")

    return LocalizationResponse(
        analysis_id=latest["id"],
        bug_report_id=report_id,
        status=latest["status"],
        repo_sha=result.get("repo_sha"),
        candidate_files=pass1.get("candidate_files", []),
        localizations=pass2.get("localizations", []) if pass2 else [],
        root_cause_hypothesis=pass2.get("root_cause_hypothesis") if pass2 else None,
        confidence=pass1.get("confidence", 0),
    )


@router.delete("/reports/{report_id}", status_code=204)
def delete_report(
    report_id: str,
    _key: str = Depends(require_api_key),
) -> None:
    """Delete a bug report."""
    if not report_delete(report_id):
        raise HTTPException(status_code=404, detail="Bug report not found")
