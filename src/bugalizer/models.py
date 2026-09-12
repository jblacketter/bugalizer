"""Pydantic models for Bugalizer API."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


# ---------------------------------------------------------------------------
# Bug status workflow
# ---------------------------------------------------------------------------

class BugStatus(str, Enum):
    """Canonical bug status workflow.

    Phase 4 (Fix Proposals) adds `fix_proposing` as a transient claim state
    between `triaged` (post-localization) and `fix_proposed` so the fix
    stage can acquire exclusive access via a real compare-and-set.
    """
    SUBMITTED = "submitted"
    VALIDATING = "validating"
    TRIAGED = "triaged"
    ANALYZING = "analyzing"
    CLARIFICATION_NEEDED = "clarification_needed"
    FIX_PROPOSING = "fix_proposing"       # transient claim for the fix stage
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
        BugStatus.ANALYZING, BugStatus.FIX_PROPOSING,
        BugStatus.DEFERRED, BugStatus.DUPLICATE, BugStatus.CLOSED,
    },
    BugStatus.ANALYZING: {
        BugStatus.CLARIFICATION_NEEDED, BugStatus.FIX_PROPOSED, BugStatus.TRIAGED,
    },
    BugStatus.CLARIFICATION_NEEDED: {BugStatus.ANALYZING, BugStatus.CLOSED},
    BugStatus.FIX_PROPOSING: {BugStatus.FIX_PROPOSED, BugStatus.TRIAGED},
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
# Phase 4: unlocks fix_proposing (transient claim) + fix_proposed
#          (terminal-within-phase). Still gated: fix_approved, fix_committed,
#          verified (future Dashboard phase).
CURRENT_PHASE_TARGETS: set[BugStatus] = {
    BugStatus.VALIDATING,
    BugStatus.TRIAGED,
    BugStatus.ANALYZING,
    BugStatus.CLARIFICATION_NEEDED,
    BugStatus.FIX_PROPOSING,
    BugStatus.FIX_PROPOSED,
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
# Analysis mode (Phase 5 §5.3 — per-report analysis tier)
# ---------------------------------------------------------------------------

class AnalysisMode(str, Enum):
    """Per-report analysis-tier selection.

    Gates *automatic* queue-worker dispatch only; an explicit
    `POST /reports/{id}/analyze` always overrides the mode for that request.
    """
    AUTO = "auto"              # local triage+localize, cloud fix when eligible
    LOCAL_ONLY = "local_only"  # never auto-call the cloud; stop after localization
    HOLD = "hold"              # validate + dedupe only; human picks a tier later


class AnalysisTier(str, Enum):
    """Tier choice for a manual `POST /reports/{id}/analyze` request."""
    LOCAL = "local"   # run/re-run triage + localization on the local provider
    CLOUD = "cloud"   # run the Stage 4 fix proposal on the cloud fix provider


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
    analysis_mode: AnalysisMode = AnalysisMode.AUTO


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
    analysis_mode: str = "auto"
    resolution_reason: Optional[str] = None
    assigned_to: Optional[str] = None
    created_at: str
    updated_at: str
    warnings: list[str] = Field(default_factory=list)
    # Populated when the report has an unresolved pipeline failure (retry gate).
    failed_stage: Optional[str] = None
    last_error: Optional[str] = None
    # True when the report has at least one completed localization (i.e. it has
    # been analyzed locally) — lets the dashboard flag analyzed cards at a glance.
    localized: bool = False
    # Latest completed analysis per tier (§5b.1 scan-state chips): local =
    # ollama, cloud = any other provider (paid). Null when that tier never ran.
    last_local_analysis_at: Optional[str] = None
    last_cloud_analysis_at: Optional[str] = None


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


class AnalysisModeUpdateRequest(BaseModel):
    """Request body for changing a report's analysis mode."""
    analysis_mode: AnalysisMode


KEY_REF_PATTERN = r"^[A-Za-z0-9._:/-]+$"


class LLMOverride(BaseModel):
    """Per-request provider/model/key override for the cloud tier (Phase 7, D3).

    The key travels with the request and is used for that one call: it is
    never written to any table, never logged, never returned. `key_ref` is a
    non-secret, caller-supplied reference to the settings row that supplied
    the key (Aegis sends its AI-settings row identity) so token usage stays
    attributable without storing the key itself. Bugalizer treats it as
    opaque.
    """
    provider: Optional[str] = Field(None, min_length=1, max_length=50)
    model: Optional[str] = Field(None, min_length=1, max_length=200)
    api_key: Optional[SecretStr] = None
    key_ref: Optional[str] = Field(
        None, min_length=1, max_length=128, pattern=KEY_REF_PATTERN
    )

    def has_key(self) -> bool:
        return self.api_key is not None and bool(self.api_key.get_secret_value())


class AnalyzeRequest(BaseModel):
    """Request body for POST /reports/{id}/analyze (the dashboard's two buttons).

    `llm` is accepted with `tier: cloud` only (422 otherwise).
    """
    tier: AnalysisTier
    llm: Optional[LLMOverride] = None


class AnalyzeResponse(BaseModel):
    """202 response for a dispatched manual analysis."""
    id: str
    tier: str
    dispatched: bool = True
    detail: str = ""
    # Cloud tier only: where the provider/model came from
    # (`request`, `project`, `global`). Null for the local tier.
    llm_source: Optional[str] = None


# ---------------------------------------------------------------------------
# Project models
# ---------------------------------------------------------------------------

class IngestSource(str, Enum):
    """Where a project's bug reports are pulled from (Phase 7 seam for B1)."""
    SUPABASE = "supabase"


_TABLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SupabaseIngestConfig(BaseModel):
    """Ingest config for `ingest_source=supabase`.

    Exactly three declared fields and no extras: there is no field a
    credential could ride in. `credential_env` names the env var that holds
    the credential on the host; the credential itself is never stored.
    """
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=1000)
    table: str = Field(..., min_length=1, max_length=128)
    credential_env: str = Field(..., min_length=1, max_length=128)

    @field_validator("url")
    @classmethod
    def _credential_free_https(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise ValueError("url must use https")
        if not parts.hostname:
            raise ValueError("url must include a host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("url must not embed credentials (userinfo)")
        if parts.query or parts.fragment:
            raise ValueError("url must not carry a query string or fragment")
        return value

    @field_validator("table")
    @classmethod
    def _table_identifier(cls, value: str) -> str:
        if not _TABLE_IDENT.match(value):
            raise ValueError("table must be a plain SQL identifier")
        return value

    @field_validator("credential_env")
    @classmethod
    def _env_var_name(cls, value: str) -> str:
        if not _ENV_VAR_NAME.match(value):
            raise ValueError(
                "credential_env must be an environment variable name "
                "(uppercase letters, digits, underscores)"
            )
        return value


def validate_ingest_pair(
    source: Optional[str], config: Optional[dict[str, Any]]
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Single checkpoint for the ingest invariant: both null, or a known
    source with a config that validates for that source.

    Returns the normalized `(source, config)` pair. Raises ValueError with a
    fixed, non-echoing message on any other combination.
    """
    if source is None and config is None:
        return None, None
    if source is None:
        raise ValueError("ingest_config requires ingest_source")
    if config is None:
        raise ValueError("ingest_source requires ingest_config")
    try:
        source_enum = IngestSource(source)
    except ValueError:
        raise ValueError("unknown ingest_source") from None
    if source_enum is IngestSource.SUPABASE:
        try:
            validated = SupabaseIngestConfig.model_validate(config)
        except Exception as exc:  # pydantic ValidationError
            # Report field names and reasons only, never input values.
            reasons = "; ".join(
                f"{'.'.join(str(p) for p in err.get('loc', ())) or 'config'}: {err.get('msg', 'invalid')}"
                for err in getattr(exc, "errors", lambda: [])()
            ) or "invalid ingest_config"
            raise ValueError(f"invalid ingest_config for supabase ({reasons})") from None
        return source_enum.value, validated.model_dump()
    raise ValueError("unknown ingest_source")  # pragma: no cover - enum-exhaustive


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    repo_url: str = Field(..., min_length=1, max_length=1000)
    default_branch: str = "main"
    # Local stages ONLY (triage + localization). Never consulted by Stage 4.
    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5-coder:7b"
    # Stage 4 fix-proposal override (§5.3). NULL = use the global
    # fix_provider / default_fix_model. Separate namespace from llm_* so a
    # default `ollama` project never routes cloud fixes to Ollama.
    fix_llm_provider: Optional[str] = None
    fix_llm_model: Optional[str] = None
    # Phase 7: ingest seam for B1. Both null or both set (validate_ingest_pair).
    ingest_source: Optional[str] = None
    ingest_config: Optional[dict[str, Any]] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    repo_url: Optional[str] = None
    default_branch: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    # Explicit null clears the override (falls back to global fix settings).
    fix_llm_provider: Optional[str] = None
    fix_llm_model: Optional[str] = None
    # Explicit null on either clears both (see api/projects.update_project).
    ingest_source: Optional[str] = None
    ingest_config: Optional[dict[str, Any]] = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    repo_url: str
    repo_path: Optional[str] = None
    default_branch: str
    llm_provider: str
    llm_model: str
    fix_llm_provider: Optional[str] = None
    fix_llm_model: Optional[str] = None
    ingest_source: Optional[str] = None
    ingest_config: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
    total: int


# ---------------------------------------------------------------------------
# Queue models
# ---------------------------------------------------------------------------

class FailedReport(BaseModel):
    """A report parked with an unresolved pipeline failure."""
    id: str
    title: str
    failed_stage: str
    last_error: Optional[str] = None
    permanent: bool = False


class QueueOverview(BaseModel):
    """Status counts for the bug report queue."""
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    failed: list[FailedReport] = Field(default_factory=list)


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

class UsageAttribution(BaseModel):
    """Usage grouped by who paid: the key source and the caller's key_ref
    (Phase 7). `key_source` is `request` or `env`; rows written before
    Phase 7 carry null for both."""
    key_source: Optional[str] = None
    key_ref: Optional[str] = None
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0


class UsageSummary(BaseModel):
    """Token usage summary."""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_estimated_cost_usd: float = 0.0
    by_provider: dict[str, dict] = Field(default_factory=dict)
    # Additive (Phase 7): the same totals, grouped by key_source + key_ref.
    attribution: list[UsageAttribution] = Field(default_factory=list)


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
