"""Tests for the pipeline modules (validator, triage, orchestrator)."""

import os
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer.db import init_db, report_get, analyses_for_report
from bugalizer.pipeline.validator import extract_structured_data, find_duplicate, validate_report
from bugalizer.pipeline.triage import triage_report, _parse_triage_response
from bugalizer.pipeline.orchestrator import process_submitted, process_triaged
from bugalizer.llm.client import LLMResponse


@pytest.fixture(autouse=True)
def fresh_db():
    from bugalizer import db
    db._conn = None
    os.environ["BUGALIZER_DB_PATH"] = ":memory:"
    from bugalizer.config import settings
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    settings.duplicate_threshold = 0.8
    init_db()
    yield


def _make_project():
    from bugalizer.db import project_create
    return project_create(name="Test", repo_url="https://github.com/test/repo")


def _make_report(project_id, title="Button broken", description="The submit button does not work"):
    from bugalizer.db import report_create
    return report_create(
        project_id=project_id,
        title=title,
        description=description,
        reporter="tester@example.com",
    )


# ---------------------------------------------------------------------------
# Structured data extraction
# ---------------------------------------------------------------------------

def test_extract_urls():
    data = extract_structured_data("See https://example.com/page and http://foo.bar/baz")
    assert len(data["urls"]) == 2
    assert "https://example.com/page" in data["urls"]


def test_extract_file_paths():
    data = extract_structured_data("Error in src/bugalizer/main.py:42 when loading")
    assert any("src/bugalizer/main.py:42" in p for p in data["file_paths"])


def test_extract_stack_trace():
    text = "Traceback (most recent call last):\n  File \"app.py\", line 10"
    data = extract_structured_data(text)
    assert data["has_stack_trace"] is True


def test_extract_error_messages():
    text = "Got ValueError: invalid literal for int()"
    data = extract_structured_data(text)
    assert len(data["error_messages"]) >= 1


def test_no_stack_trace():
    data = extract_structured_data("The button is blue instead of red")
    assert data["has_stack_trace"] is False


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def test_find_duplicate_match():
    proj = _make_project()
    _make_report(proj["id"], "Login button broken", "The login button does not work at all")
    dup = find_duplicate(
        "Login button broken", "The login button does not work at all",
        proj["id"],
    )
    assert dup is not None


def test_find_duplicate_no_match():
    proj = _make_project()
    _make_report(proj["id"], "Login button broken", "The login button does not work at all")
    dup = find_duplicate(
        "Server crashes on startup", "The server throws an OOM error on boot",
        proj["id"],
    )
    assert dup is None


def test_find_duplicate_excludes_self():
    proj = _make_project()
    report = _make_report(proj["id"], "Login broken", "Cannot log in")
    dup = find_duplicate(
        "Login broken", "Cannot log in",
        proj["id"], exclude_id=report["id"],
    )
    assert dup is None


# ---------------------------------------------------------------------------
# Validation pipeline
# ---------------------------------------------------------------------------

def test_validate_report_no_duplicate():
    proj = _make_project()
    report = _make_report(proj["id"])
    result = validate_report(report)
    assert result["validation_passed"] is True
    assert result["duplicate_of"] is None


def test_validate_report_duplicate():
    proj = _make_project()
    _make_report(proj["id"], "Button broken", "The submit button does not work")
    report2 = _make_report(proj["id"], "Button broken", "The submit button does not work")
    result = validate_report(report2)
    assert result["validation_passed"] is False
    assert result["duplicate_of"] is not None


# ---------------------------------------------------------------------------
# Triage response parsing
# ---------------------------------------------------------------------------

def test_parse_triage_json():
    raw = '{"severity": "high", "category": "ui", "feature_area": null, "summary": "test", "needs_clarification": false, "clarification_questions": [], "confidence": 0.9}'
    result = _parse_triage_response(raw)
    assert result["severity"] == "high"
    assert result["confidence"] == 0.9


def test_parse_triage_json_with_markdown():
    raw = '```json\n{"severity": "low", "category": "api", "feature_area": null, "summary": "test", "needs_clarification": false, "clarification_questions": [], "confidence": 0.5}\n```'
    result = _parse_triage_response(raw)
    assert result["severity"] == "low"


# ---------------------------------------------------------------------------
# Triage with mocked LLM
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_triage_report_success():
    proj = _make_project()
    report = _make_report(proj["id"])

    mock_response = LLMResponse(
        content=json.dumps({
            "severity": "high",
            "category": "ui",
            "feature_area": "forms",
            "summary": "Submit button is non-functional",
            "needs_clarification": False,
            "clarification_questions": [],
            "confidence": 0.85,
        }),
        prompt_tokens=150,
        completion_tokens=50,
        model="ollama/qwen2.5-coder:7b",
        provider="ollama",
    )

    # Report needs to be in triaged status for triage to make sense
    from bugalizer.db import report_update_status
    report_update_status(report["id"], "triaged")
    report = report_get(report["id"])

    with patch("bugalizer.pipeline.triage.complete", new_callable=AsyncMock, return_value=mock_response):
        result = await triage_report(report)

    assert result["severity"] == "high"
    assert result["category"] == "ui"

    # Check analysis was created
    analyses = analyses_for_report(report["id"], phase="triage")
    assert len(analyses) == 1
    assert analyses[0]["status"] == "completed"
    assert analyses[0]["prompt_tokens"] == 150

    # Check report fields updated
    updated = report_get(report["id"])
    assert updated["severity"] == "high"
    assert updated["feature_area"] == "forms"


@pytest.mark.asyncio
async def test_triage_report_needs_clarification():
    proj = _make_project()
    report = _make_report(proj["id"])

    mock_response = LLMResponse(
        content=json.dumps({
            "severity": "medium",
            "category": "api",
            "feature_area": None,
            "summary": "Unclear bug",
            "needs_clarification": True,
            "clarification_questions": ["What browser are you using?"],
            "confidence": 0.3,
        }),
        prompt_tokens=100,
        completion_tokens=40,
        model="ollama/qwen2.5-coder:7b",
        provider="ollama",
    )

    from bugalizer.db import report_update_status
    report_update_status(report["id"], "triaged")
    report = report_get(report["id"])

    with patch("bugalizer.pipeline.triage.complete", new_callable=AsyncMock, return_value=mock_response):
        result = await triage_report(report)

    assert result["needs_clarification"] is True
    updated = report_get(report["id"])
    assert updated["status"] == "clarification_needed"


@pytest.mark.asyncio
async def test_triage_report_failure():
    proj = _make_project()
    report = _make_report(proj["id"])

    from bugalizer.db import report_update_status
    report_update_status(report["id"], "triaged")
    report = report_get(report["id"])

    with patch("bugalizer.pipeline.triage.complete", new_callable=AsyncMock, side_effect=Exception("LLM timeout")):
        with pytest.raises(Exception, match="LLM timeout"):
            await triage_report(report)

    # Analysis should be failed
    analyses = analyses_for_report(report["id"], phase="triage")
    assert len(analyses) == 1
    assert analyses[0]["status"] == "failed"

    # Report should be back to triaged
    updated = report_get(report["id"])
    assert updated["status"] == "triaged"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_submitted_validates_and_triages():
    proj = _make_project()
    report = _make_report(proj["id"])
    assert report["status"] == "submitted"

    await process_submitted(report["id"])

    updated = report_get(report["id"])
    assert updated["status"] == "triaged"

    # Validation analysis created
    analyses = analyses_for_report(report["id"], phase="validation")
    assert len(analyses) == 1
    assert analyses[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_process_submitted_detects_duplicate():
    proj = _make_project()
    original = _make_report(proj["id"], "Login broken", "Cannot log in to the system")
    # Manually triage the original so it's not a submitted report
    from bugalizer.db import report_update_status
    report_update_status(original["id"], "triaged")

    dup = _make_report(proj["id"], "Login broken", "Cannot log in to the system")
    await process_submitted(dup["id"])

    updated = report_get(dup["id"])
    assert updated["status"] == "duplicate"
    assert "duplicate_of:" in (updated.get("resolution_reason") or "")


@pytest.mark.asyncio
async def test_process_submitted_claim_prevents_double():
    """Two concurrent calls — only one should process."""
    proj = _make_project()
    report = _make_report(proj["id"])

    # First call claims it
    await process_submitted(report["id"])
    # Second call finds it already claimed
    await process_submitted(report["id"])

    updated = report_get(report["id"])
    assert updated["status"] == "triaged"

    # Only one validation analysis
    analyses = analyses_for_report(report["id"], phase="validation")
    assert len(analyses) == 1


@pytest.mark.asyncio
async def test_process_triaged_with_mocked_llm():
    proj = _make_project()
    report = _make_report(proj["id"])

    # Get through Stage 1 first
    await process_submitted(report["id"])
    assert report_get(report["id"])["status"] == "triaged"

    mock_response = LLMResponse(
        content=json.dumps({
            "severity": "medium",
            "category": "ui",
            "feature_area": "buttons",
            "summary": "Button does not submit form",
            "needs_clarification": False,
            "clarification_questions": [],
            "confidence": 0.9,
        }),
        prompt_tokens=120,
        completion_tokens=45,
        model="ollama/qwen2.5-coder:7b",
        provider="ollama",
    )

    with patch("bugalizer.pipeline.triage.complete", new_callable=AsyncMock, return_value=mock_response):
        await process_triaged(report["id"])

    updated = report_get(report["id"])
    assert updated["status"] == "triaged"  # stays triaged (enriched)
    assert updated["feature_area"] == "buttons"


# ---------------------------------------------------------------------------
# Manual local analysis (run_local_analysis) — no re-triage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_local_analysis_skips_retriage_when_already_triaged():
    """Manual 'Analyze (local)' on a triaged report must NOT re-run triage
    (the conservative local model would just re-flag clarification and bounce
    it back) — it goes straight to localization."""
    from bugalizer.db import report_update_status, analysis_create
    from bugalizer.pipeline import orchestrator

    project = _make_project()
    report = _make_report(project["id"])
    report_update_status(report["id"], "triaged")
    analysis_create(report["id"], "triage", "completed",
                    result={"summary": "s", "needs_clarification": False})

    with patch.object(orchestrator, "process_triaged", new_callable=AsyncMock) as m_triage, \
         patch.object(orchestrator, "process_localization", new_callable=AsyncMock) as m_local:
        await orchestrator.run_local_analysis(report["id"])

    m_triage.assert_not_awaited()
    m_local.assert_awaited_once_with(report["id"])


@pytest.mark.asyncio
async def test_run_local_analysis_pushes_clarification_needed_to_triaged():
    """A clarification_needed report is moved to 'triaged' and localized, not
    re-triaged."""
    from bugalizer.db import report_update_status, analysis_create
    from bugalizer.pipeline import orchestrator

    project = _make_project()
    report = _make_report(project["id"])
    report_update_status(report["id"], "triaged")
    analysis_create(report["id"], "triage", "completed",
                    result={"summary": "s", "needs_clarification": True})
    report_update_status(report["id"], "clarification_needed")

    with patch.object(orchestrator, "process_triaged", new_callable=AsyncMock) as m_triage, \
         patch.object(orchestrator, "process_localization", new_callable=AsyncMock) as m_local:
        await orchestrator.run_local_analysis(report["id"])

    assert report_get(report["id"])["status"] == "triaged"
    m_triage.assert_not_awaited()
    m_local.assert_awaited_once_with(report["id"])
