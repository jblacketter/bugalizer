"""Tests for Stage 4 (fix_proposer)."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer import db
from bugalizer.config import settings
from bugalizer.db import (
    analysis_create,
    fix_proposals_for_report,
    init_db,
    project_create,
    report_create,
    report_get,
    report_update_status,
)
from bugalizer.llm.client import LLMResponse
from bugalizer.pipeline.fix_proposer import (
    FixProposalError,
    _extract_json,
    _validate_proposal,
    propose_fix,
)


@pytest.fixture(autouse=True)
def fresh_db():
    db._conn = None
    os.environ["BUGALIZER_DB_PATH"] = ":memory:"
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    settings.anthropic_api_key = "test-key"
    init_db()
    yield


def _seed_fixture_report(tmp_path):
    """Create a project + triaged report + completed localization analysis."""
    project = project_create(name="demo", repo_url="https://example.com/r.git")
    # Point the project at a fake repo dir containing one file.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "app.py").write_text(
        "def divide(a, b):\n    return a / b\n", encoding="utf-8"
    )
    db.project_update(project["id"], repo_path=str(repo_root))

    report = report_create(
        project_id=project["id"],
        title="Zero-division crash in divide()",
        description="Call divide(1,0) — crashes with ZeroDivisionError.",
        reporter="qa@example.com",
        severity="high",
    )
    report_update_status(report["id"], "triaged")

    # Insert a completed localization analysis in the REAL Stage-3 schema
    # (see pipeline/localizer.py:180-185): result = {"pass1": {...},
    # "pass2": {...}, "repo_sha": ...}.
    analysis = analysis_create(
        bug_report_id=report["id"],
        phase="localization",
        status="completed",
        result={
            "pass1": {
                "candidate_files": [
                    {"path": "app.py", "relevance": 0.95, "reason": "divide() lives here"},
                ],
                "confidence": 0.9,
            },
            "pass2": {
                "localizations": [
                    {
                        "file": "app.py",
                        "function": "divide",
                        "line_range": [1, 2],
                        "confidence": 0.9,
                        "reason": "divide by zero happens here",
                    }
                ],
                "root_cause_hypothesis": "divide() does not check b != 0",
            },
            "repo_sha": "deadbeef",
        },
        llm_provider="ollama",
        llm_model="qwen2.5-coder:7b",
    )
    return report, analysis


def _make_llm_response_for_proposal(payload: dict) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(payload),
        prompt_tokens=100,
        completion_tokens=50,
        model="anthropic/claude-sonnet-4-6",
        provider="anthropic",
    )


def _valid_proposal_payload() -> dict:
    return {
        "root_cause": "divide() lacks a guard for b == 0",
        "explanation": "Add an early return or raise a typed error when b is 0.",
        "diff": (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,4 @@\n"
            " def divide(a, b):\n"
            "+    if b == 0:\n"
            "+        raise ValueError('b must not be zero')\n"
            "     return a / b\n"
        ),
        "confidence": 0.82,
        "files_changed": ["app.py"],
    }


# ---------------------------------------------------------------------------
# Pure-function tests — no DB, no LLM.
# ---------------------------------------------------------------------------

def test_extract_json_plain_object():
    out = _extract_json('{"a": 1, "b": 2}')
    assert out == {"a": 1, "b": 2}


def test_extract_json_strips_fences():
    out = _extract_json('```json\n{"a": 1}\n```')
    assert out == {"a": 1}


def test_extract_json_finds_first_object_in_prose():
    out = _extract_json('Here is the proposal:\n{"a": 1}\nThanks!')
    assert out == {"a": 1}


def test_extract_json_raises_when_no_object():
    with pytest.raises(FixProposalError):
        _extract_json("no json here")


def test_validate_proposal_happy_path():
    norm = _validate_proposal(_valid_proposal_payload())
    assert norm["confidence"] == pytest.approx(0.82)
    assert norm["files_changed"] == ["app.py"]


def test_validate_proposal_rejects_out_of_range_confidence():
    p = _valid_proposal_payload()
    p["confidence"] = 1.5
    with pytest.raises(FixProposalError, match="in \\[0.0, 1.0\\]"):
        _validate_proposal(p)


def test_validate_proposal_rejects_empty_diff():
    p = _valid_proposal_payload()
    p["diff"] = "   "
    with pytest.raises(FixProposalError, match="empty"):
        _validate_proposal(p)


def test_validate_proposal_rejects_non_diff_string():
    p = _valid_proposal_payload()
    p["diff"] = "just change app.py to guard against zero"
    with pytest.raises(FixProposalError, match="not a unified diff"):
        _validate_proposal(p)


def test_validate_proposal_rejects_diff_missing_hunk_header():
    p = _valid_proposal_payload()
    p["diff"] = "--- a/x.py\n+++ b/x.py\n+added\n"
    with pytest.raises(FixProposalError, match="not a unified diff"):
        _validate_proposal(p)


def test_validate_proposal_rejects_missing_keys():
    p = _valid_proposal_payload()
    del p["root_cause"]
    with pytest.raises(FixProposalError, match="root_cause"):
        _validate_proposal(p)


# ---------------------------------------------------------------------------
# End-to-end propose_fix tests — DB in memory, LLM mocked.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_fix_happy_path(tmp_path):
    report, analysis = _seed_fixture_report(tmp_path)

    with patch(
        "bugalizer.pipeline.fix_proposer.llm_client.complete",
        new=AsyncMock(return_value=_make_llm_response_for_proposal(_valid_proposal_payload())),
    ):
        await propose_fix(report["id"])

    refreshed = report_get(report["id"])
    assert refreshed["status"] == "fix_proposed"

    proposals = fix_proposals_for_report(report["id"])
    assert len(proposals) == 1
    p = proposals[0]
    assert p["analysis_id"] == analysis["id"]
    assert p["confidence"] == pytest.approx(0.82)
    assert p["files_changed"] == ["app.py"]
    assert p["status"] == "proposed"
    assert "divide" in p["diff"]


@pytest.mark.asyncio
async def test_propose_fix_idempotent_on_same_analysis(tmp_path):
    """Second run for the same (report, analysis) must not create a duplicate."""
    report, analysis = _seed_fixture_report(tmp_path)

    with patch(
        "bugalizer.pipeline.fix_proposer.llm_client.complete",
        new=AsyncMock(return_value=_make_llm_response_for_proposal(_valid_proposal_payload())),
    ):
        await propose_fix(report["id"])

    # Reset status so the claim could succeed again.
    report_update_status(report["id"], "triaged")

    with patch(
        "bugalizer.pipeline.fix_proposer.llm_client.complete",
        new=AsyncMock(return_value=_make_llm_response_for_proposal(_valid_proposal_payload())),
    ) as mock_llm:
        await propose_fix(report["id"])
        # Second run detected the existing proposal and skipped the LLM call.
        mock_llm.assert_not_called()

    proposals = fix_proposals_for_report(report["id"])
    assert len(proposals) == 1
    assert report_get(report["id"])["status"] == "fix_proposed"


@pytest.mark.asyncio
async def test_propose_fix_llm_failure_returns_to_triaged(tmp_path):
    report, _ = _seed_fixture_report(tmp_path)

    with patch(
        "bugalizer.pipeline.fix_proposer.llm_client.complete",
        new=AsyncMock(side_effect=RuntimeError("network down")),
    ):
        await propose_fix(report["id"])

    assert report_get(report["id"])["status"] == "triaged"
    assert fix_proposals_for_report(report["id"]) == []


@pytest.mark.asyncio
async def test_propose_fix_malformed_llm_output_returns_to_triaged(tmp_path):
    report, _ = _seed_fixture_report(tmp_path)
    bad = LLMResponse(
        content="not json at all",
        prompt_tokens=10,
        completion_tokens=5,
        model="anthropic/claude-sonnet-4-6",
        provider="anthropic",
    )

    with patch(
        "bugalizer.pipeline.fix_proposer.llm_client.complete",
        new=AsyncMock(return_value=bad),
    ):
        await propose_fix(report["id"])

    assert report_get(report["id"])["status"] == "triaged"
    assert fix_proposals_for_report(report["id"]) == []


@pytest.mark.asyncio
async def test_propose_fix_rejects_non_diff_llm_output(tmp_path):
    """LLM returns valid JSON but the diff field is not a unified diff;
    the stage must reject it and return the report to triaged with no
    proposal row persisted."""
    report, _ = _seed_fixture_report(tmp_path)
    bad_payload = _valid_proposal_payload()
    bad_payload["diff"] = "change divide() to guard against zero"
    bad = _make_llm_response_for_proposal(bad_payload)

    with patch(
        "bugalizer.pipeline.fix_proposer.llm_client.complete",
        new=AsyncMock(return_value=bad),
    ):
        await propose_fix(report["id"])

    assert report_get(report["id"])["status"] == "triaged"
    assert fix_proposals_for_report(report["id"]) == []


@pytest.mark.asyncio
async def test_propose_fix_no_localization_returns_to_triaged(tmp_path):
    # Seed a report in triaged status but NO localization analysis.
    project = project_create(name="empty", repo_url="https://example.com/r.git")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    db.project_update(project["id"], repo_path=str(repo_root))
    report = report_create(
        project_id=project["id"],
        title="T",
        description="D",
        reporter="q@e.com",
        severity="low",
    )
    report_update_status(report["id"], "triaged")

    await propose_fix(report["id"])

    # Returned to triaged; no proposal created.
    assert report_get(report["id"])["status"] == "triaged"
    assert fix_proposals_for_report(report["id"]) == []
