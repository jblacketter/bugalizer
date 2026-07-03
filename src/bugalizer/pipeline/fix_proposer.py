"""Stage 4: cloud-LLM-powered fix-proposal generation.

Given a triaged report that has a completed localization analysis, call a
cloud LLM (Anthropic via litellm) with the bug, the localization evidence,
and the candidate file contents, and persist a unified-diff fix proposal
to the `fix_proposals` table.

Lifecycle:
    TRIAGED  --(atomic claim: try_claim_report)-->  FIX_PROPOSING
    FIX_PROPOSING  --(on success)-->               FIX_PROPOSED
    FIX_PROPOSING  --(on any failure)-->           TRIAGED
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from bugalizer.config import settings
from bugalizer.db import (
    analysis_create,
    db_write_lock,
    fix_proposal_create,
    fix_proposals_for_report,
    latest_completed_localization,
    project_get,
    report_get,
    report_update_status,
    token_usage_create,
    try_claim_report,
)
from bugalizer.llm import client as llm_client
from bugalizer.llm.prompts import format_fix_proposal_prompt
from bugalizer.pipeline.localizer import read_candidate_files

logger = logging.getLogger(__name__)


class FixProposalError(Exception):
    """A real fix-proposal failure — recorded as a failed `fix` analysis row.

    `permanent=True` means retrying will not help (bad LLM output, auth
    failure) and the retry gate must never re-dispatch. Defaults to permanent
    because the common raisers are output-validation failures.
    """

    def __init__(self, message: str, *, permanent: bool = True) -> None:
        super().__init__(message)
        self.permanent = permanent


class FixProposalDefer(Exception):
    """A precondition is not met (no/stale localization, no candidate files,
    project misconfig). The report is returned to `triaged` WITHOUT recording a
    failure — it is not a fix attempt, so it must not count toward the cap."""


def _classify_llm_error(exc: BaseException) -> bool:
    """Return True if an exception from the LLM call is *permanent* (no retry).

    Auth/permission/bad-request failures are permanent; timeouts, rate limits,
    and transient network/API errors are retryable. Classified by exception type
    name so we do not hard-depend on litellm's exception hierarchy.
    """
    permanent_markers = (
        "AuthenticationError",
        "PermissionDeniedError",
        "PermissionError",
        "BadRequestError",
        "NotFoundError",
        "InvalidRequestError",
    )
    name = type(exc).__name__
    if name in permanent_markers:
        return True
    # The client raises RuntimeError when the Anthropic key is missing.
    if isinstance(exc, RuntimeError) and "API_KEY" in str(exc).upper():
        return True
    return False


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a string.

    Tolerant of Claude-style responses that wrap JSON in prose or code
    fences. Raises FixProposalError if no valid object is found.
    """
    text = text.strip()
    # Strip ``` fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    # First try whole text as JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back: find first { ... matching }
    start = text.find("{")
    if start < 0:
        raise FixProposalError(f"No JSON object found in LLM response: {text[:200]!r}")
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError as exc:
                    raise FixProposalError(
                        f"Extracted candidate JSON is malformed: {exc}"
                    ) from exc
    raise FixProposalError("Unbalanced JSON braces in LLM response")


def _looks_like_unified_diff(diff: str) -> bool:
    """Best-effort structural check for unified-diff format.

    Requires at least one `--- ` file header, one `+++ ` file header, and
    one `@@` hunk header (two `@@` markers on a line, per unified-diff
    convention). Permissive about per-file ordering and per-hunk shape —
    a stricter parser would reject correct-but-weird real-model output.
    """
    has_minus = False
    has_plus = False
    has_hunk = False
    for line in diff.splitlines():
        if line.startswith("--- "):
            has_minus = True
        elif line.startswith("+++ "):
            has_plus = True
        elif line.startswith("@@") and line.count("@@") >= 2:
            has_hunk = True
        if has_minus and has_plus and has_hunk:
            return True
    return False


def _validate_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the LLM's fix-proposal JSON shape.

    Returns a normalized dict with keys (root_cause, explanation, diff,
    confidence, files_changed). Raises FixProposalError on validation
    failure.
    """
    required = ("root_cause", "explanation", "diff", "confidence", "files_changed")
    for key in required:
        if key not in payload:
            raise FixProposalError(f"Missing required key in proposal: {key!r}")

    root_cause = str(payload["root_cause"]).strip()
    explanation = str(payload["explanation"]).strip()
    diff = str(payload["diff"])
    if not diff.strip():
        raise FixProposalError("Proposal diff is empty")
    if not _looks_like_unified_diff(diff):
        raise FixProposalError(
            "Proposal diff is not a unified diff (missing `---`/`+++`/`@@` headers)"
        )

    try:
        confidence = float(payload["confidence"])
    except (TypeError, ValueError) as exc:
        raise FixProposalError(
            f"confidence must be a float, got {payload['confidence']!r}"
        ) from exc
    if not (0.0 <= confidence <= 1.0):
        raise FixProposalError(
            f"confidence must be in [0.0, 1.0], got {confidence}"
        )

    files_changed = payload["files_changed"]
    if not isinstance(files_changed, list) or not all(
        isinstance(p, str) for p in files_changed
    ):
        raise FixProposalError(
            "files_changed must be a list of strings"
        )

    return {
        "root_cause": root_cause,
        "explanation": explanation,
        "diff": diff,
        "confidence": confidence,
        "files_changed": files_changed,
    }


def _collect_candidate_files(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull candidate-file entries out of a Stage-3 localization result.

    Stage 3 persists `result = {"pass1": {...}, "pass2": {...},
    "repo_sha": ...}` (see `pipeline/localizer.py` — the combined-result
    assembly at the end of `localize_report`). pass2 is the richer
    confirmation pass: `{localizations: [{file, function, line_range,
    confidence, reason}, ...], root_cause_hypothesis: "..."}`. pass1 is
    `{candidate_files: [{path, relevance, reason}, ...], confidence}`.

    Prefer pass2 (more specific — has function + line_range). Fall back
    to pass1 if pass2 is missing or empty. Return entries normalized to
    `{path, ...}` for compatibility with `localizer.read_candidate_files`.
    """
    result = analysis.get("result")
    if not isinstance(result, dict):
        return []

    out: list[dict[str, Any]] = []

    pass2 = result.get("pass2") if isinstance(result.get("pass2"), dict) else None
    if pass2:
        for loc in pass2.get("localizations") or []:
            if not isinstance(loc, dict):
                continue
            path = loc.get("file") or loc.get("path")
            if not path:
                continue
            entry: dict[str, Any] = {"path": path}
            for k, v in loc.items():
                if k not in ("file", "path"):
                    entry[k] = v
            out.append(entry)
        if out:
            return out

    pass1 = result.get("pass1") if isinstance(result.get("pass1"), dict) else None
    if pass1:
        for cand in pass1.get("candidate_files") or []:
            if not isinstance(cand, dict):
                continue
            path = cand.get("path") or cand.get("file")
            if not path:
                continue
            entry = {"path": path}
            for k, v in cand.items():
                if k not in ("path", "file"):
                    entry[k] = v
            out.append(entry)

    return out


async def propose_fix(report_id: str) -> None:
    """Run the fix-proposal stage for a report.

    Atomically claims `triaged → fix_proposing`. On success transitions to
    `fix_proposed` and persists a `fix_proposals` row. A precondition miss
    (no/stale localization, no candidate files) defers back to `triaged`
    without recording a failure. A real failure (LLM/parse/validation) records
    a failed `fix` analysis row — marked transient or permanent — so the retry
    gate in `reports_eligible_for_fix` can cap paid re-dispatch.
    """
    # 1. Atomic claim — exits if another worker already took it.
    async with db_write_lock:
        claimed = try_claim_report(report_id, "triaged", "fix_proposing")
    if not claimed:
        logger.debug("Report %s already claimed for fix proposal", report_id)
        return

    try:
        report = report_get(report_id)
        if not report:
            logger.warning("Report %s not found after fix-claim", report_id)
            return

        # Double-check idempotency: if a proposal exists for the latest
        # localization analysis already, skip.
        analysis = latest_completed_localization(report_id)
        if not analysis:
            raise FixProposalDefer("No completed localization analysis to work from")

        existing = fix_proposals_for_report(report_id)
        if any(p.get("analysis_id") == analysis["id"] for p in existing):
            logger.info(
                "Report %s already has a fix proposal for analysis %s; skipping",
                report_id, analysis["id"],
            )
            async with db_write_lock:
                report_update_status(report_id, "fix_proposed")
            return

        project = project_get(report["project_id"])
        if not project or not project.get("repo_path"):
            raise FixProposalDefer("Report project has no repo_path; cannot read files")

        # Defensive freshness gate: never spend a paid cloud call on stale
        # file evidence. The eligibility query already filters stale
        # localization out, but re-check here in case HEAD advanced (or a
        # re-localization landed) between sampling and this claim winning.
        head_sha = project.get("head_sha")
        result = analysis.get("result")
        loc_sha = result.get("repo_sha") if isinstance(result, dict) else None
        if not head_sha or loc_sha != head_sha:
            raise FixProposalDefer(
                f"Localization is stale (repo_sha={loc_sha!r} != "
                f"project head_sha={head_sha!r}); skipping fix proposal"
            )

        # 2. Gather candidate files, bounded by size caps.
        candidates = _collect_candidate_files(analysis)
        if not candidates:
            raise FixProposalDefer("Localization produced no candidate files")

        per_file_cap = settings.fix_max_file_bytes
        file_contents: dict[str, str] = await asyncio.to_thread(
            read_candidate_files,
            project["repo_path"],
            candidates,
            max_files=settings.localize_max_files,
            max_chars=per_file_cap,
        )

        # Enforce total bundle cap
        total = 0
        capped: dict[str, str] = {}
        for path, content in file_contents.items():
            remaining = settings.fix_max_bundle_bytes - total
            if remaining <= 0:
                break
            clipped = content if len(content) <= remaining else content[:remaining]
            capped[path] = clipped
            total += len(clipped)
        if not capped:
            raise FixProposalDefer("No candidate file contents available after path+size checks")

        # 3. Build prompt + call LLM.
        messages = format_fix_proposal_prompt(
            report,
            analysis,
            capped,
            enable_prompt_caching=settings.fix_enable_prompt_caching,
        )

        # §5.3: Stage 4 resolves via the project's fix_llm_provider/
        # fix_llm_model (nullable → global fix_provider/default_fix_model).
        # It never reads the local llm_provider/llm_model pair, so a default
        # `ollama` project still routes fixes to the cloud provider.
        fix_provider, fix_model = llm_client.resolve_fix_llm(project)
        llm_response = await llm_client.complete(
            model=fix_model,
            messages=messages,
            provider=fix_provider,
        )

        # 4. Parse + validate.
        payload = _extract_json(llm_response.content)
        validated = _validate_proposal(payload)

        # 5. Persist (under lock).
        async with db_write_lock:
            analysis_create(
                bug_report_id=report_id,
                phase="fix",
                status="completed",
                result=validated,
                llm_provider=llm_response.provider,
                llm_model=llm_response.model,
                prompt_tokens=llm_response.prompt_tokens,
                completion_tokens=llm_response.completion_tokens,
            )
            fix_proposal_create(
                bug_report_id=report_id,
                analysis_id=analysis["id"],
                root_cause=validated["root_cause"],
                explanation=validated["explanation"],
                diff=validated["diff"],
                confidence=validated["confidence"],
                files_changed=validated["files_changed"],
            )
            token_usage_create(
                project_id=report["project_id"],
                provider=llm_response.provider,
                model=llm_response.model,
                bug_report_id=report_id,
                prompt_tokens=llm_response.prompt_tokens,
                completion_tokens=llm_response.completion_tokens,
            )
            report_update_status(report_id, "fix_proposed")

        logger.info(
            "Report %s fix proposal created (confidence=%.2f, files=%d)",
            report_id, validated["confidence"], len(validated["files_changed"]),
        )

    except FixProposalDefer as exc:
        # Precondition not met — not a fix attempt. Return to triaged WITHOUT
        # recording a failure, so it never counts toward max_fix_retries.
        logger.info("Fix proposal deferred for report %s: %s", report_id, exc)
        async with db_write_lock:
            try_claim_report(report_id, "fix_proposing", "triaged")

    except Exception as exc:
        # A real fix attempt failed. Record a failed `fix` analysis row so the
        # retry gate can cap re-dispatch, and classify transient vs permanent
        # (permanent = never retry — bad output or auth failure).
        if isinstance(exc, FixProposalError):
            permanent = exc.permanent
        else:
            permanent = _classify_llm_error(exc)
        logger.error(
            "Fix proposal failed for report %s (permanent=%s): %s",
            report_id, permanent, exc,
        )
        async with db_write_lock:
            analysis_create(
                bug_report_id=report_id,
                phase="fix",
                status="failed",
                result={"error": str(exc), "permanent": permanent},
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            try_claim_report(report_id, "fix_proposing", "triaged")
