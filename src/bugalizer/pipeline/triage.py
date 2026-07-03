"""Stage 2: Triage & classification (Ollama via litellm)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from bugalizer.db import (
    analysis_create,
    analysis_update,
    db_write_lock,
    project_get,
    report_update_fields,
    report_update_status,
    token_usage_create,
)
from bugalizer.llm.client import complete, resolve_local_llm
from bugalizer.llm.prompts import format_triage_prompt

logger = logging.getLogger(__name__)


def _parse_triage_response(content: str) -> dict[str, Any]:
    """Parse JSON from LLM response, handling markdown code blocks."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (```json and ```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)


async def triage_report(
    report: dict[str, Any],
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Run Stage 2 triage on a report.

    Calls the LLM (lock NOT held during network I/O), then writes results
    to DB under db_write_lock.

    Provider/model default to the project's local `llm_provider`/`llm_model`
    (falling back to the global triage default) — see
    `llm.client.resolve_local_llm`. Explicit arguments win.

    Returns the analysis result dict.
    """
    if model is None or provider is None:
        resolved_provider, resolved_model = resolve_local_llm(
            project_get(report["project_id"]), stage="triage"
        )
        provider = provider or resolved_provider
        model = model or resolved_model

    now = datetime.now(timezone.utc).isoformat()

    # Create a pending analysis record (DB write under lock)
    async with db_write_lock:
        analysis = analysis_create(
            bug_report_id=report["id"],
            phase="triage",
            status="running",
            started_at=now,
        )

    try:
        # LLM call — NO lock held during network I/O
        messages = format_triage_prompt(report)
        llm_response = await complete(model=model, messages=messages, provider=provider)

        triage_result = _parse_triage_response(llm_response.content)
        completed_at = datetime.now(timezone.utc).isoformat()

        # All DB writes under lock
        async with db_write_lock:
            analysis_update(
                analysis["id"],
                status="completed",
                result=triage_result,
                llm_provider=llm_response.provider,
                llm_model=llm_response.model,
                prompt_tokens=llm_response.prompt_tokens,
                completion_tokens=llm_response.completion_tokens,
                completed_at=completed_at,
            )

            update_fields: dict[str, Any] = {}
            if triage_result.get("severity"):
                update_fields["severity"] = triage_result["severity"]
            if triage_result.get("feature_area"):
                update_fields["feature_area"] = triage_result["feature_area"]
            if update_fields:
                report_update_fields(report["id"], **update_fields)

            token_usage_create(
                project_id=report["project_id"],
                provider=llm_response.provider,
                model=llm_response.model,
                bug_report_id=report["id"],
                prompt_tokens=llm_response.prompt_tokens,
                completion_tokens=llm_response.completion_tokens,
            )

            if triage_result.get("needs_clarification"):
                report_update_status(report["id"], "clarification_needed")
            else:
                report_update_status(report["id"], "triaged")

        return triage_result

    except Exception as e:
        logger.error("Triage failed for report %s: %s", report["id"], e)
        completed_at = datetime.now(timezone.utc).isoformat()
        async with db_write_lock:
            analysis_update(
                analysis["id"],
                status="failed",
                result={"error": str(e)},
                completed_at=completed_at,
            )
            report_update_status(report["id"], "triaged")
        raise
