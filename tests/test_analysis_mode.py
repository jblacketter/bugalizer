"""Tests for Phase 5 §5.3: per-report analysis tier + provider/model resolution.

Covers:
- `analysis_mode` gating of the auto-dispatch eligibility queries
  (hold blocks all LLM stages; local_only blocks Stage 4 only).
- The manual `POST /reports/{id}/analyze` endpoint (tier=local|cloud),
  including the mode override and the cloud freshness 409s.
- `PATCH /reports/{id}/analysis_mode`.
- Provider/model resolution: local stages read project `llm_*`;
  Stage 4 reads project `fix_llm_*` → global fix settings, never `llm_*`.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer import db
from bugalizer.config import settings
from bugalizer.db import (
    analysis_create,
    init_db,
    localization_eligible_reports,
    project_create,
    project_update,
    report_create,
    report_get,
    report_update_status,
    reports_eligible_for_fix,
    triage_eligible_reports,
)
from bugalizer.llm.client import LLMResponse, resolve_fix_llm, resolve_local_llm
from bugalizer.main import app
from bugalizer.pipeline.triage import triage_report

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    db._conn = None
    os.environ["BUGALIZER_DB_PATH"] = ":memory:"
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    init_db()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project(**overrides):
    kwargs = {"name": "demo", "repo_url": "https://example.com/r.git"}
    kwargs.update(overrides)
    return project_create(**kwargs)


def _triaged_report(project, mode="auto", with_triage=True, title="bug"):
    """A report in 'triaged' status, optionally with a completed triage row."""
    report = report_create(
        project_id=project["id"],
        title=title,
        description="something broke",
        reporter="qa@example.com",
        analysis_mode=mode,
    )
    report_update_status(report["id"], "triaged")
    if with_triage:
        analysis_create(bug_report_id=report["id"], phase="triage", status="completed")
    return report


def _add_fresh_localization(report_id, sha="deadbeef"):
    """Completed localization whose repo_sha matches head_sha='deadbeef'."""
    return analysis_create(
        bug_report_id=report_id,
        phase="localization",
        status="completed",
        result={
            "pass1": {"candidate_files": [{"path": "app.py"}], "confidence": 0.9},
            "pass2": None,
            "repo_sha": sha,
        },
    )


# ---------------------------------------------------------------------------
# Mode gating: eligibility queries
# ---------------------------------------------------------------------------

def test_hold_blocks_triage_eligibility():
    project = _project()
    held = _triaged_report(project, mode="hold", with_triage=False)
    auto = _triaged_report(project, mode="auto", with_triage=False)

    ids = {r["id"] for r in triage_eligible_reports()}
    assert auto["id"] in ids
    assert held["id"] not in ids


def test_hold_blocks_localization_eligibility():
    project = _project()
    project_update(project["id"], repo_path="/tmp/fake-repo", head_sha="deadbeef")
    held = _triaged_report(project, mode="hold")
    auto = _triaged_report(project, mode="auto")

    ids = {r["id"] for r in localization_eligible_reports()}
    assert auto["id"] in ids
    assert held["id"] not in ids


def test_local_only_localizes_but_never_fix_eligible():
    """local_only runs the local stages but is excluded from Stage 4."""
    project = _project()
    project_update(project["id"], repo_path="/tmp/fake-repo", head_sha="deadbeef")

    local_only = _triaged_report(project, mode="local_only")
    auto = _triaged_report(project, mode="auto")

    # Localization (a local stage) still auto-dispatches for local_only.
    loc_ids = {r["id"] for r in localization_eligible_reports()}
    assert local_only["id"] in loc_ids

    # With a fresh localization, only the auto report reaches Stage 4.
    _add_fresh_localization(local_only["id"])
    _add_fresh_localization(auto["id"])
    fix_ids = {r["id"] for r in reports_eligible_for_fix()}
    assert auto["id"] in fix_ids
    assert local_only["id"] not in fix_ids


def test_hold_never_fix_eligible():
    project = _project()
    project_update(project["id"], repo_path="/tmp/fake-repo", head_sha="deadbeef")
    held = _triaged_report(project, mode="hold")
    _add_fresh_localization(held["id"])
    assert held["id"] not in {r["id"] for r in reports_eligible_for_fix()}


# ---------------------------------------------------------------------------
# API: analysis_mode on create + PATCH
# ---------------------------------------------------------------------------

def _api_project():
    r = client.post(
        "/api/v1/projects",
        json={"name": "P", "repo_url": "https://example.com/r.git"},
    )
    assert r.status_code == 201
    return r.json()


def _api_report(project_id, **extra):
    payload = {
        "title": "Crash",
        "description": "It crashed",
        "reporter": "qa@example.com",
        "project_id": project_id,
    }
    payload.update(extra)
    return client.post("/api/v1/reports", json=payload)


def test_create_report_defaults_to_auto():
    p = _api_project()
    r = _api_report(p["id"])
    assert r.status_code == 201
    assert r.json()["analysis_mode"] == "auto"


def test_create_report_with_hold_mode():
    p = _api_project()
    r = _api_report(p["id"], analysis_mode="hold")
    assert r.status_code == 201
    assert r.json()["analysis_mode"] == "hold"
    # Round-trips through GET as well.
    got = client.get(f"/api/v1/reports/{r.json()['id']}")
    assert got.json()["analysis_mode"] == "hold"


def test_create_report_invalid_mode_is_422():
    p = _api_project()
    r = _api_report(p["id"], analysis_mode="cloud_always")
    assert r.status_code == 422


def test_patch_analysis_mode():
    p = _api_project()
    report = _api_report(p["id"]).json()
    r = client.patch(
        f"/api/v1/reports/{report['id']}/analysis_mode",
        json={"analysis_mode": "local_only"},
    )
    assert r.status_code == 200
    assert r.json()["analysis_mode"] == "local_only"
    assert report_get(report["id"])["analysis_mode"] == "local_only"


def test_patch_analysis_mode_invalid_value_is_422():
    p = _api_project()
    report = _api_report(p["id"]).json()
    r = client.patch(
        f"/api/v1/reports/{report['id']}/analysis_mode",
        json={"analysis_mode": "bogus"},
    )
    assert r.status_code == 422


def test_patch_analysis_mode_unknown_report_is_404():
    r = client.patch(
        "/api/v1/reports/nope/analysis_mode", json={"analysis_mode": "hold"}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# API: POST /reports/{id}/analyze
# ---------------------------------------------------------------------------

def test_analyze_unknown_report_is_404():
    r = client.post("/api/v1/reports/nope/analyze", json={"tier": "local"})
    assert r.status_code == 404


def test_analyze_invalid_tier_is_422():
    p = _api_project()
    report = _api_report(p["id"]).json()
    r = client.post(f"/api/v1/reports/{report['id']}/analyze", json={"tier": "gpu"})
    assert r.status_code == 422


def test_analyze_local_requires_triaged_status():
    """A freshly submitted (unvalidated) report cannot be manually analyzed."""
    p = _api_project()
    report = _api_report(p["id"]).json()  # status: submitted
    r = client.post(f"/api/v1/reports/{report['id']}/analyze", json={"tier": "local"})
    assert r.status_code == 409
    assert "submitted" in r.json()["detail"]


def test_analyze_local_dispatches_and_overrides_hold():
    """tier=local on a held report dispatches — explicit action beats the mode."""
    project = _project()
    held = _triaged_report(project, mode="hold", with_triage=False)

    mock_run = AsyncMock()
    with patch("bugalizer.api.reports.run_local_analysis", mock_run):
        r = client.post(f"/api/v1/reports/{held['id']}/analyze", json={"tier": "local"})
    assert r.status_code == 202
    body = r.json()
    assert body["tier"] == "local"
    assert body["dispatched"] is True
    mock_run.assert_awaited_once_with(held["id"])
    # The mode itself is untouched — the override is one-shot.
    assert report_get(held["id"])["analysis_mode"] == "hold"


def test_analyze_cloud_without_localization_is_409():
    project = _project()
    report = _triaged_report(project)
    r = client.post(f"/api/v1/reports/{report['id']}/analyze", json={"tier": "cloud"})
    assert r.status_code == 409
    assert "localization" in r.json()["detail"].lower()


def test_analyze_cloud_with_stale_localization_is_409():
    project = _project()
    project_update(project["id"], repo_path="/tmp/fake-repo", head_sha="newsha111")
    report = _triaged_report(project)
    _add_fresh_localization(report["id"], sha="oldsha000")  # != head_sha

    r = client.post(f"/api/v1/reports/{report['id']}/analyze", json={"tier": "cloud"})
    assert r.status_code == 409
    assert "stale" in r.json()["detail"].lower()


def test_analyze_cloud_fresh_dispatches_and_overrides_local_only():
    """tier=cloud with a SHA-fresh localization dispatches Stage 4, even for
    a local_only report — the mode gates automatic dispatch only."""
    project = _project()
    project_update(project["id"], repo_path="/tmp/fake-repo", head_sha="deadbeef")
    report = _triaged_report(project, mode="local_only")
    _add_fresh_localization(report["id"])

    mock_fix = AsyncMock()
    with patch("bugalizer.api.reports.process_fix_proposal", mock_fix):
        r = client.post(
            f"/api/v1/reports/{report['id']}/analyze", json={"tier": "cloud"}
        )
    assert r.status_code == 202
    assert r.json()["tier"] == "cloud"
    mock_fix.assert_awaited_once_with(report["id"])


# ---------------------------------------------------------------------------
# Provider/model resolution
# ---------------------------------------------------------------------------

def test_resolve_local_llm_default_project_matches_globals():
    project = _project()
    assert resolve_local_llm(project, stage="triage") == (
        "ollama", settings.default_triage_model,
    )
    assert resolve_local_llm(project, stage="localize") == (
        "ollama", settings.default_localize_model,
    )


def test_resolve_local_llm_project_override_wins():
    project = _project(llm_provider="ollama", llm_model="llama3:8b")
    assert resolve_local_llm(project, stage="triage") == ("ollama", "llama3:8b")
    assert resolve_local_llm(project, stage="localize") == ("ollama", "llama3:8b")


def test_resolve_local_llm_no_project_uses_globals():
    assert resolve_local_llm(None, stage="triage") == (
        "ollama", settings.default_triage_model,
    )


def test_resolve_fix_llm_default_project_is_cloud():
    """ACCEPTANCE (§5.3): a project left at the default llm_provider=ollama
    still resolves Stage 4 to the global cloud fix settings — never Ollama."""
    project = _project()  # llm_provider='ollama', fix_llm_* NULL
    assert project["llm_provider"] == "ollama"
    assert project["fix_llm_provider"] is None
    assert resolve_fix_llm(project) == (settings.fix_provider, settings.default_fix_model)
    assert resolve_fix_llm(project) == ("anthropic", "claude-sonnet-4-6")


def test_resolve_fix_llm_project_override_honored():
    """ACCEPTANCE (§5.3): a project fix_llm_* override is honored by Stage 4."""
    project = _project(fix_llm_provider="openai", fix_llm_model="gpt-4o")
    assert resolve_fix_llm(project) == ("openai", "gpt-4o")


def test_resolve_fix_llm_model_only_override_keeps_global_provider():
    project = _project(fix_llm_model="claude-opus-4-8")
    assert resolve_fix_llm(project) == (settings.fix_provider, "claude-opus-4-8")


def test_resolve_fix_llm_never_reads_local_llm_fields():
    """Even an exotic local llm_* pair must not leak into Stage 4 resolution."""
    project = _project(llm_provider="ollama", llm_model="qwen2.5-coder:32b")
    assert resolve_fix_llm(project) == (settings.fix_provider, settings.default_fix_model)


# ---------------------------------------------------------------------------
# Stage wiring: the resolved values reach the LLM client
# ---------------------------------------------------------------------------

def _triage_llm_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps({"severity": "low", "summary": "meh"}),
        prompt_tokens=10,
        completion_tokens=5,
        model="ollama/custom-model:1b",
        provider="ollama",
    )


async def test_triage_uses_project_local_model():
    project = _project(llm_model="custom-model:1b")
    report = _triaged_report(project, with_triage=False)

    mock_complete = AsyncMock(return_value=_triage_llm_response())
    with patch("bugalizer.pipeline.triage.complete", mock_complete):
        await triage_report(report_get(report["id"]))

    kwargs = mock_complete.await_args.kwargs
    assert kwargs["model"] == "custom-model:1b"
    assert kwargs["provider"] == "ollama"


async def test_triage_explicit_model_arg_still_wins():
    project = _project(llm_model="custom-model:1b")
    report = _triaged_report(project, with_triage=False)

    mock_complete = AsyncMock(return_value=_triage_llm_response())
    with patch("bugalizer.pipeline.triage.complete", mock_complete):
        await triage_report(report_get(report["id"]), model="explicit:7b")

    assert mock_complete.await_args.kwargs["model"] == "explicit:7b"


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_migration_adds_s53_columns_to_legacy_schema():
    """_migrate() adds analysis_mode + fix_llm_* to a pre-§5.3 database, and
    pre-existing report rows read back as mode 'auto' (today's behavior)."""
    import sqlite3

    db._conn = None
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, repo_url TEXT NOT NULL,
            repo_path TEXT, head_sha TEXT, default_branch TEXT DEFAULT 'main',
            llm_provider TEXT DEFAULT 'ollama',
            llm_model TEXT DEFAULT 'qwen2.5-coder:7b',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE bug_reports (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            title TEXT NOT NULL, description TEXT NOT NULL, reporter TEXT NOT NULL,
            steps_to_reproduce TEXT, expected_behavior TEXT, actual_behavior TEXT,
            url TEXT, feature_area TEXT, severity TEXT DEFAULT 'medium',
            environment TEXT, attachments TEXT, labels TEXT,
            status TEXT NOT NULL DEFAULT 'submitted',
            resolution_reason TEXT, assigned_to TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO projects (id, name, repo_url, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("p1", "Legacy", "https://example.com/r.git", now, now),
    )
    conn.execute(
        "INSERT INTO bug_reports (id, project_id, title, description, reporter, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("r1", "p1", "Old bug", "desc", "me", "triaged", now, now),
    )
    conn.commit()

    db._migrate(conn)

    proj_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    br_cols = {r[1] for r in conn.execute("PRAGMA table_info(bug_reports)").fetchall()}
    assert {"fix_llm_provider", "fix_llm_model"} <= proj_cols
    assert "analysis_mode" in br_cols

    # Pre-existing rows behave like today: mode auto, fix override cleared.
    row = conn.execute("SELECT analysis_mode FROM bug_reports WHERE id='r1'").fetchone()
    assert row["analysis_mode"] == "auto"
    row = conn.execute(
        "SELECT fix_llm_provider, fix_llm_model FROM projects WHERE id='p1'"
    ).fetchone()
    assert row["fix_llm_provider"] is None and row["fix_llm_model"] is None
