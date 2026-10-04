"""The board lane of a report (Phase 12 / B4, plan §1).

Bugalizer owns the mapping from its pipeline state to sonicgrid's six board
lanes, so sonicgrid never re-derives pipeline logic. Pure: it reads the rows
the result builder already has.

`stage` is one of submitted | triage | triaged | fix | review | completed;
`stage_detail` says why the report sits there (a token, never free text).
"""

from __future__ import annotations

from typing import Any, Optional

_SUBMITTED = {"submitted", "validating"}
_TRIAGED = {"triaged", "clarification_needed", "deferred"}
_REVIEW = {"fix_proposed": "fix_ready", "fix_approved": "opening_pr", "fix_committed": "pr_open"}
_COMPLETED = {"closed", "rejected", "duplicate", "verified"}
_RUNNING_DETAIL = {"triage": "triaging", "localization": "localizing"}


def _newest(rows: list[dict[str, Any]], phase: str, status: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The newest row of a phase (optionally of a status); rows are newest first."""
    for row in rows:
        if row.get("phase") == phase and (status is None or row.get("status") == status):
            return row
    return None


def _stamp(row: Optional[dict[str, Any]], key: str = "created_at") -> str:
    return (row or {}).get(key) or ""


def _fresh(localization: dict[str, Any], project: Optional[dict[str, Any]]) -> bool:
    """The fix stage's own freshness rule: a known head equal to the
    localization's commit."""
    result = localization.get("result")
    loc_sha = result.get("repo_sha") if isinstance(result, dict) else None
    head = (project or {}).get("head_sha")
    return bool(head) and loc_sha == head


def _triaged_detail(
    report: dict[str, Any],
    analyses: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    project: Optional[dict[str, Any]],
) -> str:
    """The first match of the plan's priority list (actionable states first)."""
    status = report.get("status")
    if status == "clarification_needed":
        return "needs_clarification"
    if status == "deferred":
        return "deferred"
    if report.get("analysis_mode") == "hold":
        return "held"

    localization = _newest(analyses, "localization", "completed")
    newest_proposal = proposals[0] if proposals else None

    fix = _newest(analyses, "fix")
    if (
        fix is not None and fix.get("status") == "failed"
        and _stamp(fix) > _stamp(localization)
        and _stamp(newest_proposal) < _stamp(fix)
    ):
        return "fix_failed"

    if newest_proposal is not None and newest_proposal.get("pr_state") == "closed":
        # Cleared by anything that happened after the PR was settled.
        settled = newest_proposal.get("pr_settled_at") or _stamp(newest_proposal, "updated_at")
        if _stamp(localization) <= settled and _stamp(fix) <= settled:
            return "pr_closed"

    if localization is not None:
        return "localized" if _fresh(localization, project) else "stale"
    return "not_localized"


def stage_of(
    report: dict[str, Any],
    analyses: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    project: Optional[dict[str, Any]] = None,
) -> tuple[str, str]:
    """`(stage, stage_detail)` for one report. `analyses` and `proposals` are
    the report's rows, newest first."""
    status = report.get("status") or "submitted"
    if status in _SUBMITTED:
        return "submitted", "validating" if status == "validating" else "queued"
    if status == "analyzing":
        running = next((a for a in analyses if a.get("status") == "running"), None)
        detail = _RUNNING_DETAIL.get((running or {}).get("phase") or "", "starting")
        return "triage", detail
    if status in _TRIAGED:
        return "triaged", _triaged_detail(report, analyses, proposals, project)
    if status == "fix_proposing":
        return "fix", "proposing"
    if status in _REVIEW:
        return "review", _REVIEW[status]
    if status in _COMPLETED:
        if status in ("rejected", "duplicate"):
            return "completed", status
        reason = report.get("resolution_reason") or ""
        return "completed", "merged" if reason.startswith("pr_merged:") else "closed"
    return "submitted", "queued"
