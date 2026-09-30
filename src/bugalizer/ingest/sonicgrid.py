"""Sonicgrid poll-endpoint response parsing and report mapping (Phase 10).

Contract: `documentation/BUGALIZER-POLL-ENDPOINT.md` in the sonicgrid repo.
Pure functions only; the poller does the I/O.

`reporterEmail` is deliberately not a field of `PollReport`: unknown fields are
ignored, so the email is dropped at this boundary and never reaches the
database or a log (arbiter decision D-B).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

TITLE_MAX = 80
DESCRIPTION_MAX = 50_000          # BugReportCreate.description max_length
REPORTER_MAX = 200                # BugReportCreate.reporter max_length
TRUNCATION_MARKER = "\n\n[truncated by bugalizer ingest]"
LABELS = ["sonicgrid"]
ENVIRONMENT = "sonicgrid production"


class PollAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    fileName: Optional[str] = None
    contentType: Optional[str] = None


class PollReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    reporterName: Optional[str] = None
    attachments: list[Any] = Field(default_factory=list)


class PollPage(BaseModel):
    """The page envelope. Reports stay raw here so one malformed report is
    skipped on its own instead of failing the page."""
    model_config = ConfigDict(extra="ignore")

    reports: list[Any]
    next_cursor: Optional[str] = None


def parse_page(body: Any) -> Optional[PollPage]:
    """The page envelope, or None when the body is not one."""
    try:
        return PollPage.model_validate(body)
    except ValidationError:
        return None


def derive_title(description: str) -> str:
    """First non-empty line, whitespace collapsed, cut at a word boundary."""
    for line in description.splitlines():
        text = " ".join(line.split())
        if text:
            break
    else:
        return "(no description)"
    if len(text) <= TITLE_MAX:
        return text
    cut = text[:TITLE_MAX]
    head, sep, _ = cut.rpartition(" ")
    return (head if sep and head else cut).rstrip() + "…"


def _description(raw: Optional[str]) -> str:
    text = (raw or "").strip()
    if not text:
        return "(empty report)"
    if len(text) > DESCRIPTION_MAX:
        return text[: DESCRIPTION_MAX - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return text


def _reporter(raw: Optional[str]) -> str:
    name = " ".join((raw or "").split())[:REPORTER_MAX]
    return name or "sonicgrid user"


def _attachments(raw: list[Any]) -> Optional[list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    for item in raw:
        try:
            att = PollAttachment.model_validate(item)
        except ValidationError:
            continue
        try:
            parts = urlsplit(att.url)
            host = parts.hostname
        except ValueError:            # e.g. "https://[broken"
            continue
        if parts.scheme != "https" or not host:
            continue
        kept.append({"url": att.url, "fileName": att.fileName, "contentType": att.contentType})
    return kept or None


def map_report(raw: Any, ingest_source: str) -> Optional[dict[str, Any]]:
    """One sonicgrid report -> the row `db.ingest_commit` inserts, or None
    when it lacks what an import needs (a usable `id`)."""
    try:
        report = PollReport.model_validate(raw)
    except ValidationError:
        return None
    description = _description(report.description)
    return {
        "external_id": report.id,
        "ingest_source": ingest_source,
        "title": derive_title(report.description or ""),
        "description": description,
        "reporter": _reporter(report.reporterName),
        "attachments": _attachments(report.attachments),
        "labels": list(LABELS),
        "severity": "medium",
        "environment": ENVIRONMENT,
    }
