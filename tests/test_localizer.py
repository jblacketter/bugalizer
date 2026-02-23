"""Tests for Stage 3 localization with mocked LLM."""

import os
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer.db import (
    init_db,
    project_create,
    report_create,
    report_update_status,
    analysis_create,
    analyses_for_report,
    localization_eligible_reports,
    project_update,
)
from bugalizer.llm.client import LLMResponse
from bugalizer.pipeline.localizer import localize_report, read_candidate_files, _validate_candidate_path


@pytest.fixture(autouse=True)
def fresh_db():
    from bugalizer import db
    db._conn = None
    os.environ["BUGALIZER_DB_PATH"] = ":memory:"
    from bugalizer.config import settings
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    settings.localize_confidence_threshold = 0.5
    settings.localize_max_files = 3
    settings.localize_max_file_chars = 8000
    init_db()
    yield


def _make_project(**kwargs):
    defaults = dict(name="Test", repo_url="https://github.com/test/repo")
    defaults.update(kwargs)
    return project_create(**defaults)


def _make_report(project_id, **kwargs):
    defaults = dict(
        project_id=project_id,
        title="Login button broken",
        description="Clicking login does nothing",
        reporter="tester@example.com",
    )
    defaults.update(kwargs)
    return report_create(**defaults)


# ---------------------------------------------------------------------------
# read_candidate_files
# ---------------------------------------------------------------------------

def test_read_candidate_files(tmp_path):
    """Reads file contents up to max chars."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "src" / "utils.py").write_text("def helper():\n    return 42\n")

    candidates = [
        {"path": "src/app.py", "relevance": 0.9},
        {"path": "src/utils.py", "relevance": 0.7},
    ]
    result = read_candidate_files(str(tmp_path), candidates)
    assert "src/app.py" in result
    assert "src/utils.py" in result
    assert "def main()" in result["src/app.py"]


def test_read_candidate_files_missing(tmp_path):
    """Skips files that don't exist."""
    candidates = [{"path": "nonexistent.py", "relevance": 0.9}]
    result = read_candidate_files(str(tmp_path), candidates)
    assert len(result) == 0


def test_read_candidate_files_max_files(tmp_path):
    """Respects max_files limit."""
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.py").write_text("b")
    (tmp_path / "c.py").write_text("c")
    (tmp_path / "d.py").write_text("d")

    candidates = [
        {"path": "a.py"},
        {"path": "b.py"},
        {"path": "c.py"},
        {"path": "d.py"},
    ]
    result = read_candidate_files(str(tmp_path), candidates, max_files=2)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Path traversal protection
# ---------------------------------------------------------------------------

def test_validate_path_normal(tmp_path):
    """Normal relative paths are accepted."""
    result = _validate_candidate_path(str(tmp_path), "src/main.py")
    assert result is not None
    assert str(tmp_path) in result


def test_validate_path_rejects_absolute():
    """Absolute paths are rejected."""
    result = _validate_candidate_path("/repo", "/etc/passwd")
    assert result is None


def test_validate_path_rejects_parent_traversal():
    """Paths with .. components are rejected."""
    result = _validate_candidate_path("/repo", "../../../etc/passwd")
    assert result is None


def test_validate_path_rejects_embedded_traversal():
    """Paths with embedded .. are rejected."""
    result = _validate_candidate_path("/repo", "src/../../etc/passwd")
    assert result is None


def test_validate_path_rejects_empty():
    """Empty paths are rejected."""
    result = _validate_candidate_path("/repo", "")
    assert result is None


def test_read_candidate_files_blocks_traversal(tmp_path):
    """read_candidate_files skips paths that escape the repo root."""
    # Create a file inside the repo
    (tmp_path / "safe.py").write_text("safe content")

    # Create a file outside the repo that we should NOT be able to read
    candidates = [
        {"path": "safe.py"},
        {"path": "../../../etc/passwd"},
        {"path": "/etc/shadow"},
    ]
    result = read_candidate_files(str(tmp_path), candidates, max_files=10)
    assert "safe.py" in result
    assert "../../../etc/passwd" not in result
    assert "/etc/shadow" not in result


# ---------------------------------------------------------------------------
# localize_report — Pass 1 only (low confidence)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_localize_pass1_only():
    """Low confidence skips pass 2."""
    proj = _make_project()
    report = _make_report(proj["id"])

    pass1_json = json.dumps({
        "candidate_files": [
            {"path": "src/auth.py", "relevance": 0.8, "reason": "auth module"}
        ],
        "confidence": 0.3,  # Below threshold
    })

    mock_response = LLMResponse(
        content=pass1_json,
        prompt_tokens=100,
        completion_tokens=50,
        model="ollama/qwen2.5-coder:7b",
        provider="ollama",
    )

    with patch("bugalizer.pipeline.localizer.complete", new_callable=AsyncMock, return_value=mock_response):
        result = await localize_report(
            report, "repo map text", "abc123", "/tmp/repo",
        )

    assert result["pass1"]["confidence"] == 0.3
    assert result["pass2"] is None
    assert result["repo_sha"] == "abc123"

    # Check analysis was created
    analyses = analyses_for_report(report["id"], phase="localization")
    assert len(analyses) == 1
    assert analyses[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# localize_report — Pass 1 + Pass 2 (high confidence)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_localize_pass1_and_pass2(tmp_path):
    """High confidence triggers pass 2 with file contents."""
    proj = _make_project()
    report = _make_report(proj["id"])

    # Create a file for pass 2 to read
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "auth.py").write_text("def login():\n    pass\n")

    pass1_json = json.dumps({
        "candidate_files": [
            {"path": "src/auth.py", "relevance": 0.9, "reason": "auth module"}
        ],
        "confidence": 0.8,  # Above threshold
    })

    pass2_json = json.dumps({
        "localizations": [
            {"file": "src/auth.py", "function": "login", "line_range": [1, 2],
             "confidence": 0.85, "reason": "login handler"}
        ],
        "root_cause_hypothesis": "Login function is empty",
    })

    call_count = 0

    async def mock_complete(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResponse(
                content=pass1_json, prompt_tokens=100, completion_tokens=50,
                model="ollama/qwen2.5-coder:7b", provider="ollama",
            )
        else:
            return LLMResponse(
                content=pass2_json, prompt_tokens=200, completion_tokens=100,
                model="ollama/qwen2.5-coder:7b", provider="ollama",
            )

    with patch("bugalizer.pipeline.localizer.complete", side_effect=mock_complete):
        result = await localize_report(
            report, "repo map text", "def456", str(tmp_path),
        )

    assert result["pass1"]["confidence"] == 0.8
    assert result["pass2"] is not None
    assert result["pass2"]["root_cause_hypothesis"] == "Login function is empty"
    assert result["repo_sha"] == "def456"
    assert call_count == 2

    # Check analysis
    analyses = analyses_for_report(report["id"], phase="localization")
    assert len(analyses) == 1
    assert analyses[0]["status"] == "completed"
    assert analyses[0]["prompt_tokens"] == 300  # 100 + 200
    assert analyses[0]["completion_tokens"] == 150  # 50 + 100


# ---------------------------------------------------------------------------
# localize_report — Failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_localize_failure():
    """Failure saves failed analysis and re-raises."""
    proj = _make_project()
    report = _make_report(proj["id"])

    with patch("bugalizer.pipeline.localizer.complete", new_callable=AsyncMock,
               side_effect=RuntimeError("LLM timeout")):
        with pytest.raises(RuntimeError, match="LLM timeout"):
            await localize_report(
                report, "repo map text", "abc123", "/tmp/repo",
            )

    analyses = analyses_for_report(report["id"], phase="localization")
    assert len(analyses) == 1
    assert analyses[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# localize_report — Markdown-wrapped JSON response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_localize_markdown_json():
    """Handles JSON wrapped in markdown code blocks."""
    proj = _make_project()
    report = _make_report(proj["id"])

    pass1_json = '```json\n{"candidate_files": [], "confidence": 0.1}\n```'

    mock_response = LLMResponse(
        content=pass1_json,
        prompt_tokens=50,
        completion_tokens=25,
        model="ollama/qwen2.5-coder:7b",
        provider="ollama",
    )

    with patch("bugalizer.pipeline.localizer.complete", new_callable=AsyncMock, return_value=mock_response):
        result = await localize_report(
            report, "repo map text", "abc123", "/tmp/repo",
        )

    assert result["pass1"]["confidence"] == 0.1


# ---------------------------------------------------------------------------
# Localization eligibility
# ---------------------------------------------------------------------------

def test_localization_eligible_never_localized():
    """Report with triage but no localization is eligible."""
    proj = _make_project()
    project_update(proj["id"], repo_path="/tmp/repo")
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")
    analysis_create(report["id"], "triage", "completed")

    eligible = localization_eligible_reports()
    assert len(eligible) == 1
    assert eligible[0]["id"] == report["id"]


def test_localization_eligible_no_triage():
    """Report without completed triage is not eligible."""
    proj = _make_project()
    project_update(proj["id"], repo_path="/tmp/repo")
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")

    eligible = localization_eligible_reports()
    assert len(eligible) == 0


def test_localization_eligible_no_repo():
    """Report for project without repo_path is not eligible."""
    proj = _make_project()
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")
    analysis_create(report["id"], "triage", "completed")

    eligible = localization_eligible_reports()
    assert len(eligible) == 0


def test_localization_eligible_stale_sha():
    """Report with completed localization but different SHA is eligible."""
    proj = _make_project()
    project_update(proj["id"], repo_path="/tmp/repo", head_sha="new_sha")
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")
    analysis_create(report["id"], "triage", "completed")
    # Completed localization with old SHA — different from project.head_sha
    analysis_create(
        report["id"], "localization", "completed",
        result={"pass1": {}, "repo_sha": "old_sha"},
    )

    eligible = localization_eligible_reports()
    assert len(eligible) == 1


def test_localization_eligible_fresh_sha_skipped():
    """Report with completed localization at current SHA is NOT eligible."""
    proj = _make_project()
    project_update(proj["id"], repo_path="/tmp/repo", head_sha="current_sha")
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")
    analysis_create(report["id"], "triage", "completed")
    # Completed localization with same SHA as project.head_sha
    analysis_create(
        report["id"], "localization", "completed",
        result={"pass1": {}, "repo_sha": "current_sha"},
    )

    eligible = localization_eligible_reports()
    assert len(eligible) == 0  # Fresh — should NOT be dispatched


def test_localization_eligible_not_triaged():
    """Report not in triaged status is not eligible."""
    proj = _make_project()
    project_update(proj["id"], repo_path="/tmp/repo")
    report = _make_report(proj["id"])
    # Still in submitted status
    analysis_create(report["id"], "triage", "completed")

    eligible = localization_eligible_reports()
    assert len(eligible) == 0


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_migration_adds_head_sha_to_legacy_schema():
    """init_db() migrates a legacy projects table missing head_sha."""
    import sqlite3
    from bugalizer import db

    # Reset connection
    db._conn = None

    # Create a legacy schema DB without head_sha
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            repo_url TEXT NOT NULL,
            repo_path TEXT,
            default_branch TEXT DEFAULT 'main',
            llm_provider TEXT DEFAULT 'ollama',
            llm_model TEXT DEFAULT 'qwen2.5-coder:7b',
            api_key_encrypted TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bug_reports (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            steps_to_reproduce TEXT,
            expected_behavior TEXT,
            actual_behavior TEXT,
            reporter TEXT NOT NULL,
            url TEXT,
            feature_area TEXT,
            severity TEXT DEFAULT 'medium',
            environment TEXT,
            attachments TEXT,
            labels TEXT,
            status TEXT NOT NULL DEFAULT 'submitted',
            resolution_reason TEXT,
            assigned_to TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY,
            bug_report_id TEXT NOT NULL REFERENCES bug_reports(id),
            phase TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT,
            llm_provider TEXT,
            llm_model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0.0,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id),
            bug_report_id TEXT REFERENCES bug_reports(id),
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0.0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fix_proposals (
            id TEXT PRIMARY KEY,
            bug_report_id TEXT NOT NULL REFERENCES bug_reports(id),
            analysis_id TEXT REFERENCES analyses(id),
            branch_name TEXT,
            diff TEXT,
            explanation TEXT,
            confidence REAL,
            root_cause TEXT,
            files_changed TEXT,
            status TEXT DEFAULT 'proposed',
            reviewed_by TEXT,
            review_notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)

    # Verify head_sha does NOT exist yet
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    assert "head_sha" not in columns

    # Patch _get_conn to use our legacy DB
    db._conn = conn

    # Run init_db (should apply migration)
    db._migrate(conn)

    # Verify head_sha column now exists
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    assert "head_sha" in columns

    # Insert a project and verify eligibility query doesn't crash
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO projects (id, name, repo_url, repo_path, head_sha, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("p1", "Test", "https://github.com/t/r", "/tmp/repo", "sha1", now, now),
    )
    conn.execute(
        "INSERT INTO bug_reports (id, project_id, title, description, reporter, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "p1", "Bug", "Desc", "me", "triaged", now, now),
    )
    conn.execute(
        "INSERT INTO analyses (id, bug_report_id, phase, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("a1", "r1", "triage", "completed", now),
    )
    conn.commit()

    # This should NOT raise OperationalError
    eligible = localization_eligible_reports()
    assert len(eligible) == 1

    # Reset for other tests
    db._conn = None
