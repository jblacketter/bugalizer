"""Stage 3: LLM-based code localization."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from bugalizer.config import settings
from bugalizer.db import (
    analysis_create,
    analysis_update,
    db_write_lock,
    token_usage_create,
)
from bugalizer.llm.client import complete
from bugalizer.llm.prompts import format_localize_prompt, format_localize_confirm_prompt

logger = logging.getLogger(__name__)


def _parse_json_response(content: str) -> dict[str, Any]:
    """Parse JSON from LLM response, handling markdown code blocks."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)


def _validate_candidate_path(repo_path: str, fpath: str) -> str | None:
    """Validate a candidate file path stays inside the repo root.

    Returns the resolved full path if safe, or None if the path is
    rejected (absolute path, parent traversal, or escapes repo root).
    """
    if not fpath or os.path.isabs(fpath):
        logger.warning("Rejected absolute candidate path: %s", fpath)
        return None

    if ".." in fpath.replace("\\", "/").split("/"):
        logger.warning("Rejected candidate path with parent traversal: %s", fpath)
        return None

    full_path = os.path.normpath(os.path.join(repo_path, fpath))
    repo_root = os.path.normpath(repo_path)

    # Ensure resolved path is under repo root
    if not full_path.startswith(repo_root + os.sep) and full_path != repo_root:
        logger.warning("Rejected candidate path escaping repo root: %s -> %s", fpath, full_path)
        return None

    return full_path


def read_candidate_files(
    repo_path: str,
    candidate_files: list[dict[str, Any]],
    *,
    max_files: int | None = None,
    max_chars: int | None = None,
) -> dict[str, str]:
    """Read candidate file contents from the repo.

    This is a blocking I/O function. Call via asyncio.to_thread()
    from async contexts.

    Validates that all candidate paths stay within the repo root to
    prevent path traversal attacks from LLM-provided paths.

    Returns dict mapping file path to content.
    """
    if max_files is None:
        max_files = settings.localize_max_files
    if max_chars is None:
        max_chars = settings.localize_max_file_chars

    result: dict[str, str] = {}
    for entry in candidate_files[:max_files]:
        fpath = entry.get("path", "")
        full_path = _validate_candidate_path(repo_path, fpath)
        if full_path is None:
            continue
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars)
            result[fpath] = content
        except (OSError, IOError) as e:
            logger.debug("Could not read candidate file %s: %s", fpath, e)

    return result


async def localize_report(
    report: dict[str, Any],
    repo_map_text: str,
    repo_sha: str,
    repo_path: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    triage_summary: str | None = None,
) -> dict[str, Any]:
    """Run Stage 3 localization on a report.

    Two-pass approach:
    1. Send repo map + bug report -> LLM identifies candidate files
    2. If confidence >= threshold, read file contents -> LLM refines to functions/lines

    Provider/model are resolved by the caller (orchestrator) from the
    project's local `llm_provider`/`llm_model` via
    `llm.client.resolve_local_llm`; defaults here keep pre-§5.3 behavior.

    Returns the localization result dict.
    """
    if model is None:
        model = settings.default_localize_model
    if provider is None:
        provider = "ollama"

    now = datetime.now(timezone.utc).isoformat()

    # Create pending analysis record
    async with db_write_lock:
        analysis = analysis_create(
            bug_report_id=report["id"],
            phase="localization",
            status="running",
            started_at=now,
        )

    try:
        # --- Pass 1: Initial localization ---
        messages = format_localize_prompt(report, repo_map_text, triage_summary)
        llm_response = await complete(model=model, messages=messages, provider=provider)
        pass1_result = _parse_json_response(llm_response.content)

        total_prompt_tokens = llm_response.prompt_tokens
        total_completion_tokens = llm_response.completion_tokens

        # Log pass 1 token usage
        async with db_write_lock:
            token_usage_create(
                project_id=report["project_id"],
                provider=llm_response.provider,
                model=llm_response.model,
                bug_report_id=report["id"],
                prompt_tokens=llm_response.prompt_tokens,
                completion_tokens=llm_response.completion_tokens,
            )

        # --- Pass 2: Confirmation (if confidence >= threshold) ---
        pass2_result = None
        confidence = pass1_result.get("confidence", 0)
        candidate_files = pass1_result.get("candidate_files", [])

        if confidence >= settings.localize_confidence_threshold and candidate_files:
            import asyncio
            file_contents = await asyncio.to_thread(
                read_candidate_files, repo_path, candidate_files,
            )

            if file_contents:
                confirm_messages = format_localize_confirm_prompt(
                    report, file_contents, triage_summary,
                )
                confirm_response = await complete(
                    model=model, messages=confirm_messages, provider=provider,
                )
                pass2_result = _parse_json_response(confirm_response.content)

                total_prompt_tokens += confirm_response.prompt_tokens
                total_completion_tokens += confirm_response.completion_tokens

                # Log pass 2 token usage
                async with db_write_lock:
                    token_usage_create(
                        project_id=report["project_id"],
                        provider=confirm_response.provider,
                        model=confirm_response.model,
                        bug_report_id=report["id"],
                        prompt_tokens=confirm_response.prompt_tokens,
                        completion_tokens=confirm_response.completion_tokens,
                    )

        # Build combined result
        localization_result = {
            "pass1": pass1_result,
            "pass2": pass2_result,
            "repo_sha": repo_sha,
        }

        completed_at = datetime.now(timezone.utc).isoformat()

        # Write results under lock
        async with db_write_lock:
            analysis_update(
                analysis["id"],
                status="completed",
                result=localization_result,
                llm_provider=llm_response.provider,
                llm_model=llm_response.model,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                completed_at=completed_at,
            )

        logger.info("Localization complete for report %s (confidence=%.2f)",
                     report["id"], confidence)
        return localization_result

    except Exception as e:
        logger.error("Localization failed for report %s: %s", report["id"], e)
        completed_at = datetime.now(timezone.utc).isoformat()
        async with db_write_lock:
            analysis_update(
                analysis["id"],
                status="failed",
                result={"error": str(e)},
                completed_at=completed_at,
            )
        raise
