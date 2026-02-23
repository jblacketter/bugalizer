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

        # Run localization (LLM calls are async, file reads use to_thread internally)
        await localize_report(
            report,
            repo_map.text,
            sha,
            repo_path,
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
