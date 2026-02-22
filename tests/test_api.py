"""Tests for the Bugalizer Phase 1 API."""

import os
import pytest
from fastapi.testclient import TestClient

# Use in-memory DB for tests.
os.environ["BUGALIZER_DB_PATH"] = ":memory:"

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
    init_db()
    yield


client = TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


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


def test_phase1_blocks_analyzing():
    """Phase 1 gates AI-driven transitions: triaged -> analyzing is rejected."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    _transition(rid, "triaged")
    r = _transition(rid, "analyzing")
    assert r.status_code == 409
    assert "invalid transition" in r.json()["detail"].lower()


def test_phase1_blocks_fix_proposed():
    """Phase 1 gates AI-driven transitions: cannot reach fix_proposed."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    _transition(rid, "triaged")
    r = _transition(rid, "fix_proposed")
    assert r.status_code == 409


def test_phase1_blocks_fix_approved():
    """Phase 1 gates AI-driven transitions: cannot reach fix_approved."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    # Even if we could get to fix_proposed (we can't), fix_approved is blocked
    r = _transition(rid, "fix_approved")
    assert r.status_code == 409


def test_phase1_blocks_fix_committed():
    """Phase 1 gates AI-driven transitions: cannot reach fix_committed."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    r = _transition(rid, "fix_committed")
    assert r.status_code == 409


def test_phase1_blocks_clarification_needed():
    """Phase 1 gates AI-driven transitions: cannot reach clarification_needed."""
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    _transition(rid, "triaged")
    r = _transition(rid, "clarification_needed")
    assert r.status_code == 409


def test_phase1_blocks_verified():
    """Phase 1 gates AI-driven transitions: cannot reach verified."""
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
