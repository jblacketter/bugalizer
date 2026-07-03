"""Pipeline orchestrator — coordinates stages for a single report."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from bugalizer.db import (
    analysis_create,
    analyses_for_report,
    db_write_lock,
    project_get,
    report_get,
    report_update_status,
    try_claim_report,
)
from bugalizer.pipeline.fix_proposer import propose_fix
from bugalizer.pipeline.triage import triage_report
from bugalizer.pipeline.validator import validate_report

logger = logging.getLogger(__name__)


async def process_submitted(report_id: str) -> None:
    """Process a submitted report through Stage 1 (validation).

    Atomically claims the report (submitted -> validating), runs validation,
    then transitions to the appropriate status.
    """
    async with db_write_lock:
        claimed = try_claim_report(report_id, "submitted", "validating")
    if not claimed:
        logger.debug("Report %s already claimed for validation", report_id)
        return

    report = report_get(report_id)
    if not report:
        logger.warning("Report %s not found after claim", report_id)
        return

    try:
        now = datetime.now(timezone.utc).isoformat()
        # Validation is pure Python (no I/O), runs outside lock
        result = validate_report(report)

        # DB writes under lock
        async with db_write_lock:
            analysis_create(
                bug_report_id=report_id,
                phase="validation",
                status="completed",
                result=result,
                started_at=now,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

            if result.get("duplicate_of"):
                report_update_status(
                    report_id, "duplicate",
                    resolution_reason=f"duplicate_of:{result['duplicate_of']}",
                )
            else:
                report_update_status(report_id, "triaged")

        if result.get("duplicate_of"):
            logger.info("Report %s marked as duplicate of %s", report_id, result["duplicate_of"])
        else:
            logger.info("Report %s validated and triaged", report_id)

    except Exception as e:
        logger.error("Validation failed for report %s: %s", report_id, e)
        async with db_write_lock:
            try_claim_report(report_id, "validating", "submitted")


async def process_triaged(report_id: str) -> None:
    """Process a triaged report through Stage 2 (triage/classification).

    Atomically claims the report (triaged -> analyzing), runs LLM triage
    (lock NOT held during network I/O), then writes results under lock.
    """
    async with db_write_lock:
        claimed = try_claim_report(report_id, "triaged", "analyzing")
    if not claimed:
        logger.debug("Report %s already claimed for analysis", report_id)
        return

    report = report_get(report_id)
    if not report:
        logger.warning("Report %s not found after claim", report_id)
        return

    try:
        # triage_report handles its own DB writes internally.
        # The LLM call happens outside db_write_lock.
        await triage_report(report)
        logger.info("Report %s triage complete", report_id)

    except Exception as e:
        logger.error("Triage failed for report %s: %s", report_id, e)
        # triage_report already rolls back to triaged on failure


async def process_localization(report_id: str) -> None:
    """Process a triaged report through Stage 3 (localization).

    Uses asyncio.to_thread() for blocking git/AST/file operations.
    LLM calls are already async. DB writes are under db_write_lock.
    """
    from bugalizer.git_ops.repo import get_head_sha
    from bugalizer.pipeline.repo_map import get_repo_map_cache
    from bugalizer.pipeline.localizer import localize_report

    # Claim report for localization
    async with db_write_lock:
        claimed = try_claim_report(report_id, "triaged", "analyzing")
    if not claimed:
        logger.debug("Report %s already claimed for localization", report_id)
        return

    report = report_get(report_id)
    if not report:
        logger.warning("Report %s not found after claim", report_id)
        return

    project = project_get(report["project_id"])
    if not project or not project.get("repo_path"):
        logger.info("Report %s project has no repo, skipping localization", report_id)
        async with db_write_lock:
            report_update_status(report_id, "triaged")
        return

    repo_path = project["repo_path"]
    branch = project.get("default_branch", "main")

    try:
        # Get current HEAD SHA (blocking I/O -> to_thread)
        sha = await asyncio.to_thread(get_head_sha, repo_path)

        # Update project head_sha so eligibility query stays accurate
        from bugalizer.db import project_update as _project_update
        _project_update(project["id"], head_sha=sha)

        # Double-check: if localization is already fresh for this SHA, skip
        existing = analyses_for_report(report_id, phase="localization")
        for analysis in existing:
            if analysis.get("status") == "completed" and analysis.get("result"):
                result = analysis["result"]
                if isinstance(result, dict) and result.get("repo_sha") == sha:
                    logger.info("Report %s already has fresh localization for SHA %s",
                                report_id, sha[:8])
                    async with db_write_lock:
                        report_update_status(report_id, "triaged")
                    return

        # Build/retrieve repo map (blocking CPU/IO -> to_thread)
        cache = get_repo_map_cache()
        repo_map = await asyncio.to_thread(cache.get_or_build, project["id"], branch, repo_path)

        # Get triage summary if available
        triage_summary = None
        triage_analyses = analyses_for_report(report_id, phase="triage")
        for t in triage_analyses:
            if t.get("status") == "completed" and t.get("result"):
                triage_summary = t["result"].get("summary")
                break

        # Resolve local provider/model from the project (§5.3: llm_provider/
        # llm_model scope local stages only; Stage 4 has its own namespace).
        from bugalizer.llm.client import resolve_local_llm
        provider, model = resolve_local_llm(project, stage="localize")

        # Run localization (LLM calls are async, file reads use to_thread internally)
        await localize_report(
            report,
            repo_map.text,
            sha,
            repo_path,
            model=model,
            provider=provider,
            triage_summary=triage_summary,
        )

        # Return to triaged (enriched with localization data)
        async with db_write_lock:
            report_update_status(report_id, "triaged")

        logger.info("Report %s localization complete", report_id)

    except Exception as e:
        logger.error("Localization failed for report %s: %s", report_id, e)
        async with db_write_lock:
            report_update_status(report_id, "triaged")


async def run_local_analysis(report_id: str) -> None:
    """Manual Stage 2 + Stage 3 dispatch (POST /reports/{id}/analyze, tier=local).

    The user's intent is to get localization. Since the endpoint only admits
    reports that already have a completed triage ('triaged' or
    'clarification_needed'), we do NOT re-triage — the conservative local model
    would just re-emit the same `clarification_needed` verdict and bounce the
    report right back. Instead: move a `clarification_needed` report to
    `triaged`, then run localization. Triage runs only as a fallback when no
    completed triage exists yet.

    Deliberately does NOT consult `analysis_mode` — an explicit request
    overrides `hold`/`local_only` for this one run (§5.3).
    """
    report = report_get(report_id)
    if not report:
        return

    # Push a clarification-gated report on: the user asked to analyze it anyway.
    if report["status"] == "clarification_needed":
        async with db_write_lock:
            if try_claim_report(report_id, "clarification_needed", "analyzing"):
                report_update_status(report_id, "triaged")

    has_triage = any(
        a.get("status") == "completed"
        for a in analyses_for_report(report_id, phase="triage")
    )
    if not has_triage:
        await process_triaged(report_id)

    # If triage (re-)routed to clarification_needed, the localization claim on
    # 'triaged' fails and this is a silent no-op — correct behavior.
    await process_localization(report_id)


async def process_fix_proposal(report_id: str) -> None:
    """Stage 4 entry point — delegate to the fix-proposer stage.

    The stage owns its own atomic claim (TRIAGED -> FIX_PROPOSING) and
    its own success/failure state transitions. This wrapper exists so
    orchestrator-family callers have a consistent `process_<stage>`
    entry point, matching the shape of process_submitted / _triaged /
    _localization.
    """
    await propose_fix(report_id)
