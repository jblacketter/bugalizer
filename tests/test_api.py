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
    db.reset_conn()
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
    """GET / serves the dashboard page, no auth required for the static page
    itself (its API calls carry the key). §5b split: the page references its
    CSS/JS under the /static mount."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Bugalizer" in r.text
    assert "X-API-Key" in r.text  # the page wires the key header
    assert "/static/styles.css" in r.text
    assert "/static/app.js" in r.text


def test_dashboard_static_assets_resolve():
    """§5b link integrity: every /static href/src in the served HTML must be
    fetchable with the right content type — catches broken links after the
    dashboard.html → index.html/styles.css/app.js split (and future renames)."""
    import re

    html = client.get("/").text
    links = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
    assert len(links) >= 2  # at least the stylesheet and the script

    expected_types = {".css": ("text/css",), ".js": ("text/javascript", "application/javascript")}
    for link in links:
        r = client.get(link)
        assert r.status_code == 200, f"{link} did not resolve"
        suffix = "." + link.rsplit(".", 1)[-1]
        allowed = expected_types.get(suffix)
        if allowed:
            assert r.headers["content-type"].startswith(allowed), (
                f"{link}: unexpected content-type {r.headers['content-type']}"
            )


def test_list_reports_includes_tier_summary():
    """§5b.1 scan-state chips: list rows carry last_local_analysis_at /
    last_cloud_analysis_at, derived from completed analyses by provider
    (ollama = local; anything else = cloud). Failed/incomplete rows and
    rows without a completed_at don't count."""
    from bugalizer.db import analysis_create

    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]

    # Nothing completed yet — both null.
    row = next(x for x in client.get("/api/v1/reports").json()["reports"] if x["id"] == rid)
    assert row["last_local_analysis_at"] is None
    assert row["last_cloud_analysis_at"] is None

    analysis_create(
        bug_report_id=rid, phase="triage", status="completed",
        llm_provider="ollama", llm_model="qwen2.5-coder:7b",
        completed_at="2026-01-01T00:00:00+00:00",
    )
    analysis_create(  # later local run wins the MAX
        bug_report_id=rid, phase="localization", status="completed",
        llm_provider="ollama", llm_model="qwen2.5-coder:7b",
        completed_at="2026-01-02T00:00:00+00:00",
    )
    analysis_create(  # failed cloud run must NOT count as a cloud scan
        bug_report_id=rid, phase="fix", status="failed",
        llm_provider="anthropic", llm_model="claude-sonnet-5",
        completed_at="2026-01-03T00:00:00+00:00",
    )

    row = next(x for x in client.get("/api/v1/reports").json()["reports"] if x["id"] == rid)
    assert row["last_local_analysis_at"] == "2026-01-02T00:00:00+00:00"
    assert row["last_cloud_analysis_at"] is None

    analysis_create(
        bug_report_id=rid, phase="fix", status="completed",
        llm_provider="anthropic", llm_model="claude-sonnet-5",
        completed_at="2026-01-04T00:00:00+00:00",
    )
    # Both the list row and the single-report detail carry the summary.
    row = next(x for x in client.get("/api/v1/reports").json()["reports"] if x["id"] == rid)
    assert row["last_cloud_analysis_at"] == "2026-01-04T00:00:00+00:00"
    detail = client.get(f"/api/v1/reports/{rid}").json()
    assert detail["last_local_analysis_at"] == "2026-01-02T00:00:00+00:00"
    assert detail["last_cloud_analysis_at"] == "2026-01-04T00:00:00+00:00"


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


# ---------------------------------------------------------------------------
# Phase 7 (B0): health auth flag, validation-error secrecy, ingest fields
# ---------------------------------------------------------------------------

def test_health_reports_auth_enabled_false_when_keys_empty():
    from bugalizer.config import settings
    settings.api_keys = ""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["auth_enabled"] is False


def test_health_reports_auth_enabled_true_when_keys_set():
    from bugalizer.config import settings
    settings.api_keys = "k-one,k-two"
    try:
        r = client.get("/health", headers={"X-API-Key": "k-one"})
        assert r.status_code == 200
        assert r.json()["auth_enabled"] is True
    finally:
        settings.api_keys = ""


def test_validation_errors_never_echo_input():
    """FastAPI's default 422 copies each error's `input` (the whole body for a
    body-level error) into the response. The project-wide handler drops it."""
    r = client.post(
        "/api/v1/projects",
        json={"name": "x", "repo_url": "https://example.com/r.git", "llm_model": 12345},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["detail"], "expected at least one error entry"
    for err in body["detail"]:
        assert "input" not in err
        assert "ctx" not in err
        assert {"loc", "msg", "type"} <= set(err)
    assert "12345" not in r.text


def _triaged_report_via_api():
    from bugalizer.db import report_update_status
    pid = client.post(
        "/api/v1/projects",
        json={"name": "demo", "repo_url": "https://example.com/r.git"},
    ).json()["id"]
    rid = client.post(
        "/api/v1/reports",
        json={
            "title": "bug",
            "description": "something broke",
            "reporter": "qa@example.com",
            "project_id": pid,
        },
    ).json()["id"]
    report_update_status(rid, "triaged")
    return rid


SENTINEL_KEY = "sk-ant-SENTINEL-0123456789abcdef-DO-NOT-LEAK"


def test_analyze_override_with_local_tier_is_422_without_echo():
    rid = _triaged_report_via_api()
    r = client.post(
        f"/api/v1/reports/{rid}/analyze",
        json={"tier": "local", "llm": {"api_key": SENTINEL_KEY}},
    )
    assert r.status_code == 422
    assert "cloud" in r.json()["detail"]
    assert SENTINEL_KEY not in r.text


def test_analyze_override_invalid_tier_is_422_without_echo():
    """A pydantic-level error on `tier` must not echo the body (which carries
    the key) through the default `input` field."""
    rid = _triaged_report_via_api()
    r = client.post(
        f"/api/v1/reports/{rid}/analyze",
        json={"tier": "gpu", "llm": {"api_key": SENTINEL_KEY, "key_ref": "aegis:1"}},
    )
    assert r.status_code == 422
    assert SENTINEL_KEY not in r.text


def test_analyze_key_ref_without_api_key_is_422():
    rid = _triaged_report_via_api()
    r = client.post(
        f"/api/v1/reports/{rid}/analyze",
        json={"tier": "cloud", "llm": {"provider": "anthropic", "key_ref": "aegis:ai_settings:7"}},
    )
    assert r.status_code == 422
    assert "key_ref" in r.json()["detail"]


def test_analyze_key_ref_equal_to_key_is_422_without_echo():
    rid = _triaged_report_via_api()
    r = client.post(
        f"/api/v1/reports/{rid}/analyze",
        json={"tier": "cloud", "llm": {"api_key": "abc.def-123", "key_ref": "abc.def-123"}},
    )
    assert r.status_code == 422
    assert "abc.def-123" not in r.text


def test_analyze_key_ref_bad_charset_is_422_without_echo():
    rid = _triaged_report_via_api()
    r = client.post(
        f"/api/v1/reports/{rid}/analyze",
        json={"tier": "cloud", "llm": {"api_key": SENTINEL_KEY, "key_ref": "has space!"}},
    )
    assert r.status_code == 422
    assert SENTINEL_KEY not in r.text
    assert "has space!" not in r.text


# --- ingest_source / ingest_config -----------------------------------------

def _supabase_config(**overrides):
    cfg = {
        "url": "https://abc.supabase.co",
        "table": "bug_reports",
        "credential_env": "SONICGRID_SUPABASE_KEY",
    }
    cfg.update(overrides)
    return cfg


def _project_body(**overrides):
    data = {"name": "ingest", "repo_url": "https://example.com/r.git"}
    data.update(overrides)
    return data


def test_project_ingest_pair_roundtrip():
    r = client.post(
        "/api/v1/projects",
        json=_project_body(ingest_source="supabase", ingest_config=_supabase_config()),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ingest_source"] == "supabase"
    assert body["ingest_config"] == _supabase_config()

    got = client.get(f"/api/v1/projects/{body['id']}").json()
    assert got["ingest_source"] == "supabase"
    assert got["ingest_config"] == _supabase_config()

    listed = client.get("/api/v1/projects").json()["projects"]
    assert any(p["id"] == body["id"] and p["ingest_config"] == _supabase_config() for p in listed)


def test_project_ingest_defaults_null():
    body = client.post("/api/v1/projects", json=_project_body()).json()
    assert body["ingest_source"] is None
    assert body["ingest_config"] is None


def test_project_ingest_source_without_config_is_422():
    r = client.post("/api/v1/projects", json=_project_body(ingest_source="supabase"))
    assert r.status_code == 422
    assert "ingest_config" in r.json()["detail"]


def test_project_ingest_config_without_source_is_422():
    r = client.post("/api/v1/projects", json=_project_body(ingest_config=_supabase_config()))
    assert r.status_code == 422
    assert "ingest_source" in r.json()["detail"]


def test_project_ingest_unknown_source_is_422():
    r = client.post(
        "/api/v1/projects",
        json=_project_body(ingest_source="jira", ingest_config=_supabase_config()),
    )
    assert r.status_code == 422
    assert "unknown ingest_source" in r.json()["detail"]


def test_project_ingest_extra_secret_field_is_422_without_echo():
    r = client.post(
        "/api/v1/projects",
        json=_project_body(
            ingest_source="supabase",
            ingest_config=_supabase_config(service_key="eyJ-SECRET-VALUE"),
        ),
    )
    assert r.status_code == 422
    assert "service_key" in r.json()["detail"]
    assert "eyJ-SECRET-VALUE" not in r.text


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@abc.supabase.co",       # userinfo
        "https://abc.supabase.co/?apikey=SECRET",  # query string
        "http://abc.supabase.co",                # not https
        "https://abc.supabase.co/#frag",         # fragment
    ],
)
def test_project_ingest_bad_url_is_422(url):
    r = client.post(
        "/api/v1/projects",
        json=_project_body(ingest_source="supabase", ingest_config=_supabase_config(url=url)),
    )
    assert r.status_code == 422
    assert "url" in r.json()["detail"]
    assert "SECRET" not in r.text and "pw@" not in r.text


@pytest.mark.parametrize("env_name", ["supabase_key", "SUPABASE-KEY", "1KEY", "sk-live-abc"])
def test_project_ingest_bad_credential_env_is_422(env_name):
    r = client.post(
        "/api/v1/projects",
        json=_project_body(
            ingest_source="supabase", ingest_config=_supabase_config(credential_env=env_name)
        ),
    )
    assert r.status_code == 422
    assert "credential_env" in r.json()["detail"]


def test_project_ingest_bad_table_is_422():
    r = client.post(
        "/api/v1/projects",
        json=_project_body(
            ingest_source="supabase", ingest_config=_supabase_config(table="bug reports; drop")
        ),
    )
    assert r.status_code == 422
    assert "table" in r.json()["detail"]


def test_project_patch_config_only_validates_against_stored_source():
    pid = client.post(
        "/api/v1/projects",
        json=_project_body(ingest_source="supabase", ingest_config=_supabase_config()),
    ).json()["id"]
    new_cfg = _supabase_config(table="bugs_v2")
    r = client.patch(f"/api/v1/projects/{pid}", json={"ingest_config": new_cfg})
    assert r.status_code == 200, r.text
    assert r.json()["ingest_source"] == "supabase"
    assert r.json()["ingest_config"] == new_cfg


def test_project_patch_config_only_without_stored_source_is_422():
    pid = client.post("/api/v1/projects", json=_project_body()).json()["id"]
    r = client.patch(f"/api/v1/projects/{pid}", json={"ingest_config": _supabase_config()})
    assert r.status_code == 422
    assert "ingest_source" in r.json()["detail"]


def test_project_patch_source_only_without_config_is_422():
    pid = client.post("/api/v1/projects", json=_project_body()).json()["id"]
    r = client.patch(f"/api/v1/projects/{pid}", json={"ingest_source": "supabase"})
    assert r.status_code == 422
    assert "ingest_config" in r.json()["detail"]


def test_project_patch_pair_sets_both():
    pid = client.post("/api/v1/projects", json=_project_body()).json()["id"]
    r = client.patch(
        f"/api/v1/projects/{pid}",
        json={"ingest_source": "supabase", "ingest_config": _supabase_config()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ingest_source"] == "supabase"
    assert r.json()["ingest_config"] == _supabase_config()


@pytest.mark.parametrize("field", ["ingest_source", "ingest_config"])
def test_project_patch_null_on_either_clears_both(field):
    pid = client.post(
        "/api/v1/projects",
        json=_project_body(ingest_source="supabase", ingest_config=_supabase_config()),
    ).json()["id"]
    r = client.patch(f"/api/v1/projects/{pid}", json={field: None})
    assert r.status_code == 200, r.text
    assert r.json()["ingest_source"] is None
    assert r.json()["ingest_config"] is None
    got = client.get(f"/api/v1/projects/{pid}").json()
    assert got["ingest_source"] is None and got["ingest_config"] is None


def test_project_patch_invalid_config_leaves_row_unchanged():
    pid = client.post(
        "/api/v1/projects",
        json=_project_body(ingest_source="supabase", ingest_config=_supabase_config()),
    ).json()["id"]
    r = client.patch(
        f"/api/v1/projects/{pid}",
        json={"ingest_config": _supabase_config(url="http://plain.example")},
    )
    assert r.status_code == 422
    got = client.get(f"/api/v1/projects/{pid}").json()
    assert got["ingest_config"] == _supabase_config()


# --- running revision (deploy check) ----------------------------------------

def test_health_endpoints_report_revision_key():
    """Both health endpoints carry `revision`: a string when it can be
    established, else null. The test checkout is a git repo, so it is a sha."""
    for path in ("/health/live", "/health"):
        body = client.get(path).json()
        assert "revision" in body
        assert body["revision"] is None or (
            isinstance(body["revision"], str) and len(body["revision"]) >= 7
        )


def test_detect_revision_precedence(tmp_path):
    from bugalizer.main import detect_revision
    # Configured value wins, trimmed, and needs no checkout.
    assert detect_revision("  abc123  ", checkout_root=tmp_path) == "abc123"
    # No configured value and no checkout: unknown, never a guess.
    assert detect_revision("", checkout_root=tmp_path) is None
    # No configured value but a real checkout: git HEAD.
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[1]
    sha = detect_revision("", checkout_root=repo_root)
    assert sha is not None and len(sha) == 40


def test_runtime_revision_is_pinned_per_process():
    from bugalizer.config import settings
    from bugalizer.main import runtime_revision
    runtime_revision.cache_clear()
    settings.git_revision = "deployed-sha-1"
    try:
        assert runtime_revision() == "deployed-sha-1"
        assert client.get("/health/live").json()["revision"] == "deployed-sha-1"
        # A later change (a git pull under a running service) is not picked up
        # until the process restarts.
        settings.git_revision = "deployed-sha-2"
        assert runtime_revision() == "deployed-sha-1"
    finally:
        settings.git_revision = ""
        runtime_revision.cache_clear()


def test_health_endpoints_return_200_with_null_revision():
    """Unknown revision (unstamped image, installed package, no checkout) is
    a documented `revision: null`, not a 500. The Docker healthcheck hits
    /health/live, so a 500 here would mark a healthy service unhealthy."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from bugalizer.main import app as _app
    strict = TestClient(_app, raise_server_exceptions=False)
    with patch("bugalizer.main.runtime_revision", return_value=None):
        live = strict.get("/health/live")
        assert live.status_code == 200, live.text
        assert live.json()["revision"] is None
        assert live.json()["status"] == "ok"
        ready = strict.get("/health")
        assert ready.status_code == 200, ready.text
        assert ready.json()["revision"] is None
