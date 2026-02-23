"""Pydantic models for Bugalizer API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Bug status workflow
# ---------------------------------------------------------------------------

class BugStatus(str, Enum):
    """Canonical 13-state bug status workflow."""
    SUBMITTED = "submitted"
    VALIDATING = "validating"
    TRIAGED = "triaged"
    ANALYZING = "analyzing"
    CLARIFICATION_NEEDED = "clarification_needed"
    FIX_PROPOSED = "fix_proposed"
    FIX_APPROVED = "fix_approved"
    FIX_COMMITTED = "fix_committed"
    VERIFIED = "verified"
    CLOSED = "closed"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    DEFERRED = "deferred"


TERMINAL_STATUSES = {BugStatus.CLOSED, BugStatus.REJECTED, BugStatus.DUPLICATE}

# Valid transitions: source -> set of allowed targets.
VALID_TRANSITIONS: dict[BugStatus, set[BugStatus]] = {
    BugStatus.SUBMITTED: {BugStatus.VALIDATING, BugStatus.REJECTED, BugStatus.TRIAGED},
    BugStatus.VALIDATING: {BugStatus.TRIAGED, BugStatus.REJECTED},
    BugStatus.TRIAGED: {
        BugStatus.ANALYZING, BugStatus.DEFERRED, BugStatus.DUPLICATE, BugStatus.CLOSED,
    },
    BugStatus.ANALYZING: {
        BugStatus.CLARIFICATION_NEEDED, BugStatus.FIX_PROPOSED, BugStatus.TRIAGED,
    },
    BugStatus.CLARIFICATION_NEEDED: {BugStatus.ANALYZING, BugStatus.CLOSED},
    BugStatus.FIX_PROPOSED: {BugStatus.FIX_APPROVED, BugStatus.TRIAGED, BugStatus.CLOSED},
    BugStatus.FIX_APPROVED: {BugStatus.FIX_COMMITTED},
    BugStatus.FIX_COMMITTED: {BugStatus.VERIFIED, BugStatus.TRIAGED},
    BugStatus.VERIFIED: {BugStatus.CLOSED},
    BugStatus.DEFERRED: {BugStatus.TRIAGED},
    # Terminal states — no outbound transitions (except closed can reopen).
    BugStatus.CLOSED: set(),
    BugStatus.REJECTED: set(),
    BugStatus.DUPLICATE: set(),
}


# Phase-gated transitions. This set defines which statuses can be entered
# in the current phase. Updated each phase to unlock new states.
# Phase 2: adds analyzing, clarification_needed (local LLM pipeline).
# Still gated: fix_proposed, fix_approved, fix_committed, verified (Phase 3/4).
CURRENT_PHASE_TARGETS: set[BugStatus] = {
    BugStatus.VALIDATING,
    BugStatus.TRIAGED,
    BugStatus.ANALYZING,
    BugStatus.CLARIFICATION_NEEDED,
    BugStatus.DEFERRED,
    BugStatus.DUPLICATE,
    BugStatus.CLOSED,
    BugStatus.REJECTED,
}


def validate_transition(
    current: BugStatus,
    target: BugStatus,
    *,
    enforce_phase_gating: bool = True,
) -> bool:
    """Return True if the transition from current to target is valid.

    When enforce_phase_gating is True (default), also checks that the target
    status is in the current phase's allowed set.
    """
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        return False
    if enforce_phase_gating and target not in CURRENT_PHASE_TARGETS:
        return False
    return True


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Bug report models
# ---------------------------------------------------------------------------

class BugReportCreate(BaseModel):
    """Request body for creating a bug report.

    Hard required: title, description, reporter, project_id.
    Recommended: steps_to_reproduce, expected_behavior, actual_behavior.
    """
    title: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1, max_length=50000)
    reporter: str = Field(..., min_length=1, max_length=200)
    project_id: str = Field(..., min_length=1, max_length=100)

    steps_to_reproduce: Optional[list[str]] = None
    expected_behavior: Optional[str] = None
    actual_behavior: Optional[str] = None

    url: Optional[str] = None
    feature_area: Optional[str] = None
    severity: Severity = Severity.MEDIUM
    environment: Optional[str] = None
    labels: Optional[list[str]] = None


class BugReportResponse(BaseModel):
    id: str
    project_id: str
    title: str
    description: str
    steps_to_reproduce: Optional[list[str]] = None
    expected_behavior: Optional[str] = None
    actual_behavior: Optional[str] = None
    reporter: str
    url: Optional[str] = None
    feature_area: Optional[str] = None
    severity: str
    environment: Optional[str] = None
    labels: Optional[list[str]] = None
    status: str
    resolution_reason: Optional[str] = None
    assigned_to: Optional[str] = None
    created_at: str
    updated_at: str
    warnings: list[str] = Field(default_factory=list)


class BugReportListResponse(BaseModel):
    reports: list[BugReportResponse]
    total: int


class StatusUpdateRequest(BaseModel):
    """Request body for updating bug status."""
    status: BugStatus
    resolution_reason: Optional[str] = None


class StatusUpdateResponse(BaseModel):
    id: str
    previous_status: str
    new_status: str
    resolution_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Project models
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    repo_url: str = Field(..., min_length=1, max_length=1000)
    default_branch: str = "main"
    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5-coder:7b"


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    repo_url: Optional[str] = None
    default_branch: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    repo_url: str
    repo_path: Optional[str] = None
    default_branch: str
    llm_provider: str
    llm_model: str
    created_at: str
    updated_at: str


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
    total: int


# ---------------------------------------------------------------------------
# Queue models
# ---------------------------------------------------------------------------

class QueueOverview(BaseModel):
    """Status counts for the bug report queue."""
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Analysis models
# ---------------------------------------------------------------------------

class AnalysisResponse(BaseModel):
    id: str
    bug_report_id: str
    phase: str
    status: str
    result: Optional[dict] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str


# ---------------------------------------------------------------------------
# Usage models
# ---------------------------------------------------------------------------

class UsageSummary(BaseModel):
    """Token usage summary."""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_estimated_cost_usd: float = 0.0
    by_provider: dict[str, dict] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Repo map models
# ---------------------------------------------------------------------------

class RepoMapResponse(BaseModel):
    """API response for a repo map."""
    project_id: str
    branch: str
    sha: str
    file_count: int = 0
    text: str = ""


# ---------------------------------------------------------------------------
# Localization models
# ---------------------------------------------------------------------------

class LocalizationResponse(BaseModel):
    """API response for localization results."""
    analysis_id: str
    bug_report_id: str
    status: str
    repo_sha: Optional[str] = None
    candidate_files: list[dict] = Field(default_factory=list)
    localizations: list[dict] = Field(default_factory=list)
    root_cause_hypothesis: Optional[str] = None
    confidence: float = 0.0
