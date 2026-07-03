"""Tests for the Bugalizer API."""

import os
import pytest
from fastapi.testclient import TestClient

# Use in-memory DB for tests and disable queue worker.
os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer.main import app
from bugalizer.db import init_db


@pytest.fixture(autouse=True)
def fresh_db():
    """Re-init the DB before each test."""
    from bugalizer import db
    db._conn = None
    os.environ["BUGALIZER_DB_PATH"] = ":memory:"
    from bugalizer.config import settings
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    init_db()
    yield


client = TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_liveness():
    """Liveness probe is dependency-free and always ok when the process is up."""
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_readiness_reports_checks():
    """Readiness reports per-component checks. DB is reachable in tests, so it
    returns 200; Ollama is down (no server) so overall is 'degraded'."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["database"] is True
    # ollama + worker keys are present regardless of their state
    assert "ollama" in body["checks"]
    assert "worker" in body["checks"]
    assert body["status"] in ("ok", "degraded")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def _create_project(**overrides):
    data = {"name": "Test Project", "repo_url": "https://github.com/test/repo"}
    data.update(overrides)
    return client.post("/api/v1/projects", json=data)


def test_create_project():
    r = _create_project()
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Test Project"
    assert body["repo_url"] == "https://github.com/test/repo"
    assert body["default_branch"] == "main"
    assert body["id"]


def test_list_projects():
    _create_project(name="A")
    _create_project(name="B")
    r = client.get("/api/v1/projects")
    assert r.status_code == 200
    assert r.json()["total"] == 2


def test_get_project():
    pid = _create_project().json()["id"]
    r = client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 200
    assert r.json()["id"] == pid


def test_get_project_not_found():
    r = client.get("/api/v1/projects/nonexistent")
    assert r.status_code == 404


def test_update_project():
    pid = _create_project().json()["id"]
    r = client.patch(f"/api/v1/projects/{pid}", json={"name": "Updated"})
    assert r.status_code == 200
    assert r.json()["name"] == "Updated"


def test_project_fix_llm_fields_roundtrip():
    """§5.3: fix_llm_* fields are settable, patchable, and nullable-to-clear."""
    r = _create_project(fix_llm_provider="anthropic", fix_llm_model="claude-opus-4-8")
    assert r.status_code == 201
    body = r.json()
    assert body["fix_llm_provider"] == "anthropic"
    assert body["fix_llm_model"] == "claude-opus-4-8"

    pid = body["id"]
    r = client.patch(f"/api/v1/projects/{pid}", json={"fix_llm_model": "gpt-4o"})
    assert r.json()["fix_llm_model"] == "gpt-4o"

    # Explicit null clears the override back to the global fix settings.
    r = client.patch(
        f"/api/v1/projects/{pid}",
        json={"fix_llm_provider": None, "fix_llm_model": None},
    )
    assert r.status_code == 200
    assert r.json()["fix_llm_provider"] is None
    assert r.json()["fix_llm_model"] is None


def test_project_fix_llm_defaults_null():
    body = _create_project().json()
    assert body["fix_llm_provider"] is None
    assert body["fix_llm_model"] is None


def test_update_project_non_nullable_field_rejects_null():
    pid = _create_project().json()["id"]
    r = client.patch(f"/api/v1/projects/{pid}", json={"name": None})
    assert r.status_code == 400
    assert "name" in r.json()["detail"]


def test_delete_project():
    pid = _create_project().json()["id"]
    r = client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 204
    assert client.get(f"/api/v1/projects/{pid}").status_code == 404


def test_delete_project_with_reports_returns_409():
    pid = _create_project().json()["id"]
    _create_report(pid)
    r = client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 409
    assert "bug reports" in r.json()["detail"].lower()


def test_delete_project_succeeds_after_all_reports_soft_deleted():
    """Project delete works once all reports are soft-deleted."""
    pid = _create_project().json()["id"]
    r1 = _create_report(pid).json()["id"]
    r2 = _create_report(pid, title="Second").json()["id"]
    # Still blocked
    assert client.delete(f"/api/v1/projects/{pid}").status_code == 409
    # Soft-delete both reports
    client.delete(f"/api/v1/reports/{r1}")
    client.delete(f"/api/v1/reports/{r2}")
    # Now project delete succeeds
    r = client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 204
    assert client.get(f"/api/v1/projects/{pid}").status_code == 404


# ---------------------------------------------------------------------------
# Bug Reports
# ---------------------------------------------------------------------------

def _create_report(project_id: str, **overrides):
    data = {
        "title": "Button broken",
        "description": "The submit button does not work",
        "reporter": "jack@example.com",
        "project_id": project_id,
    }
    data.update(overrides)
    return client.post("/api/v1/reports", json=data)


def test_create_report_minimal():
    pid = _create_project().json()["id"]
    r = _create_report(pid)
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "Button broken"
    assert body["status"] == "submitted"
    assert len(body["warnings"]) == 3  # Missing all 3 recommended fields


def test_create_report_full():
    pid = _create_project().json()["id"]
    r = _create_report(
        pid,
        steps_to_reproduce=["Click submit", "Observe nothing happens"],
        expected_behavior="Form submits",
        actual_behavior="Nothing happens",
    )
    assert r.status_code == 201
    assert len(r.json()["warnings"]) == 0


def test_create_report_invalid_project():
    r = _create_report("nonexistent")
    assert r.status_code == 404


def test_create_report_missing_required():
    pid = _create_project().json()["id"]
    r = client.post("/api/v1/reports", json={"project_id": pid})
    assert r.status_code == 422  # Pydantic validation


def test_list_reports():
    pid = _create_project().json()["id"]
    _create_report(pid)
    _create_report(pid, title="Second bug")
    r = client.get("/api/v1/reports")
    assert r.json()["total"] == 2


def test_list_reports_filter_by_project():
    p1 = _create_project(name="P1").json()["id"]
    p2 = _create_project(name="P2").json()["id"]
    _create_report(p1)
    _create_report(p2)
    r = client.get(f"/api/v1/reports?project_id={p1}")
    assert r.json()["total"] == 1


def test_list_reports_filter_by_status():
    pid = _create_project().json()["id"]
    _create_report(pid)
    r = client.get("/api/v1/reports?status=submitted")
    assert r.json()["total"] == 1
    r = client.get("/api/v1/reports?status=triaged")
    assert r.json()["total"] == 0


def test_list_reports_pagination():
    """§5.4: limit/offset paginate; total stays the pre-pagination count."""
    pid = _create_project().json()["id"]
    for i in range(5):
        _create_report(pid, title=f"Bug {i}")

    r = client.get("/api/v1/reports?limit=2")
    body = r.json()
    assert len(body["reports"]) == 2
    assert body["total"] == 5

    r = client.get("/api/v1/reports?limit=2&offset=4")
    body = r.json()
    assert len(body["reports"]) == 1
    assert body["total"] == 5

    # Bounds enforced.
    assert client.get("/api/v1/reports?limit=0").status_code == 422
    assert client.get("/api/v1/reports?limit=501").status_code == 422
    assert client.get("/api/v1/reports?offset=-1").status_code == 422


def test_list_reports_order():
    """§5.4: order=asc|desc on created_at; invalid value is a 422."""
    pid = _create_project().json()["id"]
    first = _create_report(pid, title="First").json()["id"]
    last = _create_report(pid, title="Last").json()["id"]

    r = client.get("/api/v1/reports?order=asc")
    ids = [x["id"] for x in r.json()["reports"]]
    assert ids.index(first) < ids.index(last)

    r = client.get("/api/v1/reports")  # default desc
    ids = [x["id"] for x in r.json()["reports"]]
    assert ids.index(last) < ids.index(first)

    assert client.get("/api/v1/reports?order=sideways").status_code == 422


def test_list_reports_includes_failure_info():
    """§5.4: list rows carry failed_stage/last_error for the error badge."""
    from bugalizer.db import analysis_create

    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    analysis_create(
        bug_report_id=rid,
        phase="localization",
        status="failed",
        result={"error": "boom", "permanent": False},
        completed_at="2026-01-01T00:00:00+00:00",
    )

    rows = client.get("/api/v1/reports").json()["reports"]
    row = next(x for x in rows if x["id"] == rid)
    assert row["failed_stage"] == "localization"
    assert row["last_error"] == "boom"


def test_list_report_analyses_endpoint():
    """§5.4: detail view reads analysis rows (triage result, history)."""
    from bugalizer.db import analysis_create

    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    analysis_create(
        bug_report_id=rid, phase="triage", status="completed",
        result={"severity": "high", "summary": "null deref"},
    )

    r = client.get(f"/api/v1/reports/{rid}/analyses")
    assert r.status_code == 200
    rows = r.json()["analyses"]
    assert len(rows) == 1
    assert rows[0]["phase"] == "triage"
    assert rows[0]["result"]["summary"] == "null deref"

    # Phase filter + 404.
    assert client.get(f"/api/v1/reports/{rid}/analyses?phase=fix").json()["analyses"] == []
    assert client.get("/api/v1/reports/nope/analyses").status_code == 404


# ---------------------------------------------------------------------------
# Dashboard (§5.4)
# ---------------------------------------------------------------------------

def test_dashboard_served_at_root():
    """GET / serves the self-contained dashboard page, no auth required for
    the static page itself (its API calls carry the key)."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Bugalizer" in r.text
    assert "X-API-Key" in r.text  # the page wires the key header


def test_get_report():
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    r = client.get(f"/api/v1/reports/{rid}")
    assert r.status_code == 200


def test_delete_report_is_soft_delete():
    """DELETE is a soft delete — report excluded from list/queue but still in DB."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    r = client.delete(f"/api/v1/reports/{rid}")
    assert r.status_code == 204
    # Not in list
    r = client.get("/api/v1/reports")
    assert r.json()["total"] == 0
    # Not in queue counts
    r = client.get("/api/v1/queue")
    assert r.json()["total"] == 0
    # But still fetchable by ID (shows as rejected/deleted)
    r = client.get(f"/api/v1/reports/{rid}")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert r.json()["resolution_reason"] == "deleted"


# ---------------------------------------------------------------------------
# Status Transitions
# ---------------------------------------------------------------------------

def _transition(report_id: str, status: str, reason: str | None = None):
    body = {"status": status}
    if reason:
        body["resolution_reason"] = reason
    return client.patch(f"/api/v1/reports/{report_id}/status", json=body)


def test_valid_transition_submitted_to_triaged():
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    r = _transition(rid, "triaged")
    assert r.status_code == 200
    assert r.json()["new_status"] == "triaged"


def test_valid_transition_submitted_to_rejected():
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    r = _transition(rid, "rejected", reason="not_a_bug")
    assert r.status_code == 200
    assert r.json()["new_status"] == "rejected"
    assert r.json()["resolution_reason"] == "not_a_bug"


def test_invalid_transition():
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    # submitted -> fix_proposed is not valid (not even in full map)
    r = _transition(rid, "fix_proposed")
    assert r.status_code == 409


def test_phase2_allows_analyzing():
    """Phase 2 unlocks triaged -> analyzing."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    _transition(rid, "triaged")
    r = _transition(rid, "analyzing")
    assert r.status_code == 200
    assert r.json()["new_status"] == "analyzing"


def test_phase2_allows_clarification_needed():
    """Phase 2 unlocks analyzing -> clarification_needed."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    _transition(rid, "triaged")
    _transition(rid, "analyzing")
    r = _transition(rid, "clarification_needed")
    assert r.status_code == 200
    assert r.json()["new_status"] == "clarification_needed"


def test_phase2_blocks_fix_proposed():
    """Phase 2 still gates fix_proposed (Phase 3/4)."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    _transition(rid, "triaged")
    r = _transition(rid, "fix_proposed")
    assert r.status_code == 409


def test_phase2_blocks_fix_approved():
    """Phase 2 still gates fix_approved (Phase 3/4)."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    r = _transition(rid, "fix_approved")
    assert r.status_code == 409


def test_phase2_blocks_fix_committed():
    """Phase 2 still gates fix_committed (Phase 3/4)."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    r = _transition(rid, "fix_committed")
    assert r.status_code == 409


def test_phase2_blocks_verified():
    """Phase 2 still gates verified (Phase 3/4)."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    r = _transition(rid, "verified")
    assert r.status_code == 409


def test_terminal_state_blocks_transition():
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    _transition(rid, "rejected")
    # rejected is terminal
    r = _transition(rid, "triaged")
    assert r.status_code == 409


def test_full_happy_path():
    """Walk through the full Phase 1 manual workflow."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]

    assert _transition(rid, "triaged").status_code == 200
    assert _transition(rid, "deferred").status_code == 200
    assert _transition(rid, "triaged").status_code == 200
    assert _transition(rid, "closed", reason="wont_fix").status_code == 200

    # Verify final state
    report = client.get(f"/api/v1/reports/{rid}").json()
    assert report["status"] == "closed"
    assert report["resolution_reason"] == "wont_fix"


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def test_queue_overview():
    pid = _create_project().json()["id"]
    _create_report(pid)
    _create_report(pid)
    rid = _create_report(pid).json()["id"]
    _transition(rid, "triaged")

    r = client.get("/api/v1/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["by_status"]["submitted"] == 2
    assert body["by_status"]["triaged"] == 1


# ---------------------------------------------------------------------------
# Fix proposals endpoint (Stage 4 / Phase 4)
# ---------------------------------------------------------------------------

def test_list_fix_proposals_empty_when_no_proposals():
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    r = client.get(f"/api/v1/reports/{rid}/fix_proposals")
    assert r.status_code == 200
    assert r.json() == {"fix_proposals": []}


def test_list_fix_proposals_404_for_missing_report():
    r = client.get("/api/v1/reports/does-not-exist/fix_proposals")
    assert r.status_code == 404


def test_list_fix_proposals_returns_persisted_rows(tmp_path):
    """When a fix_proposal row has been written, the endpoint returns it."""
    from bugalizer.db import analysis_create, fix_proposal_create

    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]

    # Seed a completed localization analysis + a fix_proposals row pointing at it.
    analysis = analysis_create(
        bug_report_id=rid,
        phase="localization",
        status="completed",
        result={"candidate_files": [{"path": "x.py"}]},
    )
    fix_proposal_create(
        bug_report_id=rid,
        analysis_id=analysis["id"],
        root_cause="rc",
        explanation="ex",
        diff="--- a/x.py\n+++ b/x.py\n@@\n- 1\n+ 2\n",
        confidence=0.75,
        files_changed=["x.py"],
    )

    r = client.get(f"/api/v1/reports/{rid}/fix_proposals")
    assert r.status_code == 200
    body = r.json()
    assert len(body["fix_proposals"]) == 1
    row = body["fix_proposals"][0]
    assert row["confidence"] == 0.75
    assert row["files_changed"] == ["x.py"]
    assert row["analysis_id"] == analysis["id"]
    assert row["status"] == "proposed"


# ---------------------------------------------------------------------------
# Failure surfacing + CORS (Phase 5.1 / 5.2)
# ---------------------------------------------------------------------------

def _seed_failed_report(phase="fix", error="boom", permanent=True):
    from bugalizer.db import (
        project_create, report_create, report_update_status, analysis_create,
    )
    proj = project_create(name="p", repo_url="https://example.com/r.git")
    rep = report_create(project_id=proj["id"], title="Broken thing",
                        description="d", reporter="q@e.com", severity="low")
    report_update_status(rep["id"], "triaged")
    analysis_create(rep["id"], phase, "failed",
                    result={"error": error, "permanent": permanent})
    return rep


def test_report_get_includes_failed_stage():
    rep = _seed_failed_report(phase="fix", error="bad diff", permanent=True)
    r = client.get(f"/api/v1/reports/{rep['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["failed_stage"] == "fix"
    assert body["last_error"] == "bad diff"


def test_report_get_no_failure_leaves_fields_null():
    from bugalizer.db import project_create, report_create
    proj = project_create(name="p", repo_url="https://example.com/r.git")
    rep = report_create(project_id=proj["id"], title="fine", description="d",
                        reporter="q@e.com", severity="low")
    body = client.get(f"/api/v1/reports/{rep['id']}").json()
    assert body["failed_stage"] is None
    assert body["last_error"] is None


def test_queue_overview_lists_failed_reports():
    rep = _seed_failed_report(phase="localization", error="loc boom", permanent=False)
    body = client.get("/api/v1/queue").json()
    failed_ids = {f["id"]: f for f in body["failed"]}
    assert rep["id"] in failed_ids
    entry = failed_ids[rep["id"]]
    assert entry["failed_stage"] == "localization"
    assert entry["last_error"] == "loc boom"
    assert entry["permanent"] is False


def test_cors_closed_by_default():
    from bugalizer.main import create_app
    from bugalizer.config import settings
    settings.cors_origins = ""
    c = TestClient(create_app())
    r = c.get("/health/live", headers={"Origin": "http://evil.example"})
    header_names = {k.lower() for k in r.headers}
    assert "access-control-allow-origin" not in header_names


def test_cors_allows_configured_origin():
    from bugalizer.main import create_app
    from bugalizer.config import settings
    settings.cors_origins = "http://dash.local"
    try:
        c = TestClient(create_app())
        r = c.get("/health/live", headers={"Origin": "http://dash.local"})
        assert r.headers.get("access-control-allow-origin") == "http://dash.local"
    finally:
        settings.cors_origins = ""
