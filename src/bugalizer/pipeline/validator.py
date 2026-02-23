"""Stage 1: Validation & pre-processing (no LLM)."""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any, Optional

from bugalizer.config import settings
from bugalizer.db import report_list

logger = logging.getLogger(__name__)

# Regex patterns for structured data extraction.
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
_FILE_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'(])([a-zA-Z0-9_./-]+\.[a-zA-Z]{1,10}(?::\d+)?)",
    re.MULTILINE,
)
_STACK_TRACE_PATTERN = re.compile(
    r"(?:Traceback \(most recent call last\):|"
    r"^\s+at\s+|"
    r"^\s+File\s+\")",
    re.MULTILINE,
)
_ERROR_PATTERN = re.compile(
    r"(?:Error|Exception|FATAL|CRITICAL|Traceback)[\s:].{0,200}",
    re.IGNORECASE,
)


def extract_structured_data(text: str) -> dict[str, Any]:
    """Extract URLs, file paths, error messages, and stack traces from text."""
    urls = _URL_PATTERN.findall(text)
    file_paths = [m for m in _FILE_PATH_PATTERN.findall(text) if "/" in m or "\\" in m]
    has_stack_trace = bool(_STACK_TRACE_PATTERN.search(text))
    error_messages = _ERROR_PATTERN.findall(text)

    return {
        "urls": urls,
        "file_paths": file_paths,
        "has_stack_trace": has_stack_trace,
        "error_messages": error_messages[:5],  # Cap at 5
    }


def find_duplicate(
    title: str,
    description: str,
    project_id: str,
    *,
    exclude_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Find a duplicate report by fuzzy matching title + description.

    Returns the matched report dict or None.
    """
    threshold = settings.duplicate_threshold
    existing = report_list(project_id=project_id)
    new_text = f"{title}\n{description}".lower()

    best_match = None
    best_score = 0.0

    for report in existing:
        if exclude_id and report["id"] == exclude_id:
            continue
        # Skip reports that are themselves duplicates or deleted
        if report.get("status") in ("duplicate", "rejected"):
            continue

        existing_text = f"{report['title']}\n{report['description']}".lower()
        score = SequenceMatcher(None, new_text, existing_text).ratio()

        if score >= threshold and score > best_score:
            best_match = report
            best_score = score

    if best_match:
        logger.info(
            "Duplicate found: report %s matches %s (score=%.2f)",
            exclude_id or "new", best_match["id"], best_score,
        )
    return best_match


def validate_report(report: dict[str, Any]) -> dict[str, Any]:
    """Run Stage 1 validation on a report.

    Returns a validation result dict with extracted data and duplicate info.
    """
    description = report.get("description", "")
    title = report.get("title", "")

    # Extract structured data
    extracted = extract_structured_data(description)

    # Check for duplicates
    duplicate = find_duplicate(
        title, description, report["project_id"], exclude_id=report["id"]
    )

    result: dict[str, Any] = {
        "extracted_data": extracted,
        "duplicate_of": duplicate["id"] if duplicate else None,
        "validation_passed": duplicate is None,
    }

    return result
