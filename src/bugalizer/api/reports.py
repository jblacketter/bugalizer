"""Bug report CRUD endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from typing import Optional

from bugalizer.auth import require_api_key
from bugalizer.db import (
    analyses_for_report,
    fix_proposals_for_report,
    latest_completed_localization,
    project_exists,
    project_get,
    report_count,
    report_create,
    report_delete,
    report_failure_info,
    report_get,
    report_ids_with_localization,
    report_list,
    report_update_fields,
    report_update_status,
)
from bugalizer.models import (
    AnalysisModeUpdateRequest,
    AnalysisTier,
    AnalyzeRequest,
    AnalyzeResponse,
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
from bugalizer.pipeline.orchestrator import process_fix_proposal, run_local_analysis

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
        analysis_mode=row.get("analysis_mode") or "auto",
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
        analysis_mode=body.analysis_mode.value,
    )

    return _row_to_response(row, warnings)


@router.get("/reports")
def list_reports(
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    _key: str = Depends(require_api_key),
) -> BugReportListResponse:
    """List bug reports, optionally filtered by project_id and/or status.

    §5.4 dashboard shape: `limit`/`offset` paginate (limit ≤ 500; omit limit
    for the full list), `order` sorts by created_at (`desc` default). `total`
    is the pre-pagination match count. Each row includes `failed_stage` /
    `last_error` when the report has an unresolved pipeline failure.
    """
    rows = report_list(
        project_id=project_id, status=status,
        limit=limit, offset=offset, order=order,
    )
    localized_ids = report_ids_with_localization()
    responses = []
    for r in rows:
        resp = _row_to_response(r)
        resp.localized = r["id"] in localized_ids
        failure = report_failure_info(r["id"])
        if failure:
            resp.failed_stage = failure["failed_stage"]
            resp.last_error = failure["last_error"]
        responses.append(resp)
    return BugReportListResponse(
        reports=responses,
        total=report_count(project_id=project_id, status=status),
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
    resp = _row_to_response(row)
    resp.localized = latest_completed_localization(report_id) is not None
    failure = report_failure_info(report_id)
    if failure:
        resp.failed_stage = failure["failed_stage"]
        resp.last_error = failure["last_error"]
    return resp


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


@router.patch("/reports/{report_id}/analysis_mode")
def update_analysis_mode(
    report_id: str,
    body: AnalysisModeUpdateRequest,
    _key: str = Depends(require_api_key),
) -> BugReportResponse:
    """Change a report's analysis mode (auto | local_only | hold).

    The mode gates *automatic* queue-worker dispatch: `hold` blocks all LLM
    stages, `local_only` blocks the paid Stage 4. It takes effect on the next
    worker poll; a stage already in flight is not interrupted.
    """
    row = report_get(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Bug report not found")
    updated = report_update_fields(report_id, analysis_mode=body.analysis_mode.value)
    return _row_to_response(updated)


@router.post("/reports/{report_id}/analyze", status_code=202)
async def analyze_report(
    report_id: str,
    body: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    _key: str = Depends(require_api_key),
) -> AnalyzeResponse:
    """Manually dispatch analysis for a report (the dashboard's two buttons).

    - `{"tier": "local"}` — run/re-run triage + localization on the local
      provider.
    - `{"tier": "cloud"}` — run the Stage 4 fix proposal on the resolved fix
      provider/model. Requires a completed, SHA-fresh localization (409
      otherwise, matching `reports_eligible_for_fix`).

    An explicit request overrides `analysis_mode` (`hold`/`local_only`) for
    this one run — the mode gates automatic dispatch, not user actions. The
    report must be in `triaged` status; work is dispatched in the background
    (202) and progress is visible via report status / queue polling.
    """
    row = report_get(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Bug report not found")

    if body.tier == AnalysisTier.LOCAL:
        # Local (re-)analysis is a manual override: allow it from 'triaged' or
        # from 'clarification_needed' (the whole point is to push a report the
        # triage model flagged for clarification on into localization). Reject
        # only transient claim states already owned by a pipeline stage.
        if row["status"] not in ("triaged", "clarification_needed"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Report is in status '{row['status']}'; manual local "
                    "analysis requires 'triaged' or 'clarification_needed'"
                ),
            )
        background_tasks.add_task(run_local_analysis, report_id)
        detail = "Local triage + localization dispatched"
    else:
        if row["status"] != "triaged":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Report is in status '{row['status']}'; cloud analysis "
                    "requires 'triaged' (run local analysis first)"
                ),
            )
        # Cloud tier: enforce the same completed + SHA-fresh localization
        # preconditions as reports_eligible_for_fix, but as a 409 the caller
        # can act on. (propose_fix re-checks defensively after its claim.)
        loc = latest_completed_localization(report_id)
        if not loc:
            raise HTTPException(
                status_code=409,
                detail="No completed localization; run local analysis first",
            )
        project = project_get(row["project_id"])
        head_sha = project.get("head_sha") if project else None
        result = loc.get("result")
        loc_sha = result.get("repo_sha") if isinstance(result, dict) else None
        if not head_sha or loc_sha != head_sha:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Localization is stale (repo_sha={loc_sha!r} != project "
                    f"head_sha={head_sha!r}); re-run local analysis first"
                ),
            )
        background_tasks.add_task(process_fix_proposal, report_id)
        detail = "Cloud fix proposal dispatched"

    return AnalyzeResponse(id=report_id, tier=body.tier.value, detail=detail)


@router.get("/reports/{report_id}/analyses")
def list_report_analyses(
    report_id: str,
    phase: Optional[str] = Query(None),
    _key: str = Depends(require_api_key),
) -> dict:
    """List analysis rows for a report, newest first, optionally by phase.

    Read-only; feeds the dashboard detail view (triage result, failure
    history). `result` is the deserialized JSON the stage persisted.
    """
    row = report_get(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Bug report not found")
    return {"analyses": analyses_for_report(report_id, phase=phase)}


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


@router.get("/reports/{report_id}/fix_proposals")
def list_fix_proposals(
    report_id: str,
    _key: str = Depends(require_api_key),
) -> dict:
    """List fix proposals for a report, newest first.

    Returns an object `{"fix_proposals": [...]}`. Each proposal includes
    `id, analysis_id, root_cause, explanation, diff, confidence,
    files_changed, status, created_at, updated_at` (and the nullable
    review fields). Read-only; proposals are created by the Stage 4
    pipeline.
    """
    row = report_get(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Bug report not found")
    return {"fix_proposals": fix_proposals_for_report(report_id)}


@router.delete("/reports/{report_id}", status_code=204)
def delete_report(
    report_id: str,
    _key: str = Depends(require_api_key),
) -> None:
    """Delete a bug report."""
    if not report_delete(report_id):
        raise HTTPException(status_code=404, detail="Bug report not found")
