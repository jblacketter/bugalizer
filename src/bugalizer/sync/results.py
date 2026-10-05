"""Result payloads for sonicgrid's `PUT /results/{bugId}` (Phase 11 / B3).

Pure functions: build the full current picture of one report, field by field
against the contract (sonicgrid `documentation/BUGALIZER-TRIAGE-ENDPOINTS.md`,
"Push a result"), and fingerprint it. Every object is strict (an unknown key
is a 400 there), and a value that would break a limit is truncated or nulled
here rather than sent. The whole payload is pushed every time, never a patch;
sonicgrid filters admin fields on read (E3), so B3 sends every field.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Optional

from bugalizer.models import AnalysisMode, BugStatus, Severity
from bugalizer.sync.stage import stage_of

SUMMARY_MAX = 2_000
CATEGORY_MAX = 50
LONG_TEXT_MAX = 20_000
CANDIDATES_MAX = 50
PATH_MAX = 500
REASON_MAX = 2_000
DIFF_MAX_BYTES = 256 * 1024
PR_URL_MAX = 500
BRANCH_MAX = 255
MODEL_MAX = 120

_STATUSES = {s.value for s in BugStatus}
_SEVERITIES = {s.value for s in Severity}
_MODES = {m.value for m in AnalysisMode}
_PR_STATES = {"open", "merged", "closed"}


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _unit(value: Any) -> Optional[float]:
    """A number in 0..1, or None (bools and NaN are not confidences)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or value < 0 or value > 1:
        return None
    return float(value)


def _latest_completed(analyses: list[dict[str, Any]], phase: str) -> Optional[dict[str, Any]]:
    for a in analyses:  # newest first
        if a.get("phase") == phase and a.get("status") == "completed":
            return a
    return None


def _model_label(analysis: Optional[dict[str, Any]]) -> Optional[str]:
    """`<provider>/<model>` of what actually ran (B5). `llm_model` is the litellm
    string, already `ollama/…` / `anthropic/…` for the built-in providers, so the
    provider is prefixed only when it is not there yet."""
    model = (analysis or {}).get("llm_model")
    if not isinstance(model, str) or not model:
        return None
    provider = (analysis or {}).get("llm_provider")
    if isinstance(provider, str) and provider and not model.startswith(provider + "/"):
        model = f"{provider}/{model}"
    return model[:MODEL_MAX]


def _fix_analysis(
    analyses: list[dict[str, Any]], proposal: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """The `fix` analysis that produced `proposal`. Not `proposal.analysis_id`:
    that is the localization the fix was built on. fix_proposer writes the fix
    analysis and then its proposal back to back under one claim, and `_now()` is
    strictly monotonic, so it is the newest completed fix analysis created no
    later than the proposal."""
    created = (proposal or {}).get("created_at")
    if not isinstance(created, str) or not created:
        return None
    for a in analyses:  # newest first
        if (a.get("phase") == "fix" and a.get("status") == "completed"
                and isinstance(a.get("created_at"), str) and a["created_at"] <= created):
            return a
    return None


def _candidates(localization: Optional[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
    result = (localization or {}).get("result")
    pass1 = result.get("pass1") if isinstance(result, dict) else None
    raw = pass1.get("candidate_files") if isinstance(pass1, dict) else None
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        path = _text(item.get("path") or item.get("file"), PATH_MAX)
        if path is None:
            continue
        out.append({
            "path": path,
            "relevance": _unit(item.get("relevance")),
            "reason": _text(item.get("reason"), REASON_MAX),
        })
        if len(out) == CANDIDATES_MAX:
            break
    return out


def _hypothesis(localization: Optional[dict[str, Any]]) -> Optional[str]:
    result = (localization or {}).get("result")
    pass2 = result.get("pass2") if isinstance(result, dict) else None
    if not isinstance(pass2, dict):
        return None
    return _text(pass2.get("root_cause_hypothesis"), LONG_TEXT_MAX)


def _diff(value: Any) -> Optional[str]:
    # A truncated diff is worse than none: over the limit, send null.
    if not isinstance(value, str) or not value:
        return None
    return value if len(value.encode("utf-8")) <= DIFF_MAX_BYTES else None


def _pr_url(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.startswith("https://") and len(value) <= PR_URL_MAX:
        return value
    return None


def build_payload(
    report: dict[str, Any],
    analyses: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    project: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """The contract body minus `revision`. `analyses` and `proposals` are the
    report's rows, newest first (as db.analyses_for_report /
    db.fix_proposals_for_report return them). `project` gives the head
    commit the board's `stale`/`localized` detail is judged against (B4)."""
    triage = _latest_completed(analyses, "triage")
    triage_result = triage.get("result") if triage and isinstance(triage.get("result"), dict) else {}
    localization = _latest_completed(analyses, "localization")
    proposal = proposals[0] if proposals else None
    recorded = next((p for p in proposals if p.get("pr_url")), None)

    pr = None
    if recorded and isinstance(recorded.get("pr_number"), int) and recorded["pr_number"] > 0:
        pr = {
            "number": recorded["pr_number"],
            "branch": (recorded.get("branch_name") or "")[:BRANCH_MAX] or None,
        }
        if pr["branch"] is None:
            pr = None

    status = report.get("status")
    stage, stage_detail = stage_of(report, analyses, proposals, project)
    pr_state = (recorded or {}).get("pr_state")
    severity = report.get("severity")
    mode = report.get("analysis_mode")

    stamps = [report.get("updated_at") or report.get("created_at") or ""]
    for a in analyses:
        stamps += [a.get("completed_at") or "", a.get("created_at") or ""]
    for p in proposals:
        stamps.append(p.get("updated_at") or "")

    return {
        "bugalizerUpdatedAt": max(s for s in stamps if isinstance(s, str)),
        "public": {
            "pipelineStatus": status if status in _STATUSES else "submitted",
            "summary": _text(triage_result.get("summary"), SUMMARY_MAX),
            "severity": severity if severity in _SEVERITIES else None,
            "prUrl": _pr_url(recorded.get("pr_url")) if recorded else None,
            # B4: the board lane; sonicgrid's status follows `stage` on an
            # applied push (contract "Status sync").
            "stage": stage,
            "stageDetail": stage_detail,
            "prState": pr_state if pr_state in _PR_STATES else None,
        },
        "admin": {
            "analysisMode": mode if mode in _MODES else None,
            "localized": localization is not None,
            "category": _text(triage_result.get("category"), CATEGORY_MAX),
            "triageConfidence": _unit(triage_result.get("confidence")),
            "rootCause": _text((proposal or {}).get("root_cause"), LONG_TEXT_MAX),
            "rootCauseHypothesis": _hypothesis(localization),
            "explanation": _text((proposal or {}).get("explanation"), LONG_TEXT_MAX),
            "candidateFiles": _candidates(localization),
            "diff": _diff((proposal or {}).get("diff")),
            "fixConfidence": _unit((proposal or {}).get("confidence")),
            "pr": pr,
            # B5: which model produced each part (contract, Phase 52).
            "triageModel": _model_label(triage),
            "localizationModel": _model_label(localization),
            "fixModel": _model_label(_fix_analysis(analyses, proposal)),
        },
    }


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(payload: dict[str, Any]) -> str:
    """Hash of the whole payload except `revision`, `bugalizerUpdatedAt`
    included: a newer analysis with identical output still pushes."""
    body = {k: v for k, v in payload.items() if k != "revision"}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
