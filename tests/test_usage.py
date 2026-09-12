"""Tests for usage endpoints and retry endpoint."""

import os
import pytest
from fastapi.testclient import TestClient

os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer.main import app
from bugalizer.db import init_db, token_usage_create, report_update_status


@pytest.fixture(autouse=True)
def fresh_db():
    from bugalizer import db
    db.reset_conn()
    os.environ["BUGALIZER_DB_PATH"] = ":memory:"
    from bugalizer.config import settings
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    init_db()
    yield


client = TestClient(app)


def _create_project(**overrides):
    data = {"name": "Test Project", "repo_url": "https://github.com/test/repo"}
    data.update(overrides)
    return client.post("/api/v1/projects", json=data)


def _create_report(project_id, **overrides):
    data = {
        "title": "Button broken",
        "description": "The submit button does not work",
        "reporter": "jack@example.com",
        "project_id": project_id,
    }
    data.update(overrides)
    return client.post("/api/v1/reports", json=data)


# ---------------------------------------------------------------------------
# Usage endpoints
# ---------------------------------------------------------------------------

def test_usage_empty():
    r = client.get("/api/v1/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["total_prompt_tokens"] == 0
    assert body["total_completion_tokens"] == 0
    assert body["total_estimated_cost_usd"] == 0.0


def test_usage_with_data():
    pid = _create_project().json()["id"]
    token_usage_create(
        project_id=pid,
        provider="ollama",
        model="qwen2.5-coder:7b",
        prompt_tokens=100,
        completion_tokens=50,
        estimated_cost_usd=0.0,
    )
    token_usage_create(
        project_id=pid,
        provider="ollama",
        model="qwen2.5-coder:7b",
        prompt_tokens=200,
        completion_tokens=75,
        estimated_cost_usd=0.0,
    )

    r = client.get("/api/v1/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["total_prompt_tokens"] == 300
    assert body["total_completion_tokens"] == 125


def test_usage_per_project():
    p1 = _create_project(name="P1").json()["id"]
    p2 = _create_project(name="P2").json()["id"]
    token_usage_create(project_id=p1, provider="ollama", model="m1", prompt_tokens=100, completion_tokens=50)
    token_usage_create(project_id=p2, provider="ollama", model="m1", prompt_tokens=500, completion_tokens=200)

    r = client.get(f"/api/v1/usage/{p1}")
    assert r.status_code == 200
    body = r.json()
    assert body["total_prompt_tokens"] == 100

    r = client.get(f"/api/v1/usage/{p2}")
    body = r.json()
    assert body["total_prompt_tokens"] == 500


# ---------------------------------------------------------------------------
# Retry endpoint
# ---------------------------------------------------------------------------

def test_retry_not_found():
    r = client.post("/api/v1/queue/nonexistent/retry")
    assert r.status_code == 404


def test_retry_wrong_status():
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    # Report is in 'submitted' status
    r = client.post(f"/api/v1/queue/{rid}/retry")
    assert r.status_code == 409
    assert "triaged" in r.json()["detail"]


def test_retry_success():
    pid = _create_project().json()["id"]
    rid = _create_report(pid).json()["id"]
    # Transition to triaged
    client.patch(f"/api/v1/reports/{rid}/status", json={"status": "triaged"})

    # Add a failed analysis
    from bugalizer.db import analysis_create
    analysis_create(rid, "triage", "failed")

    r = client.post(f"/api/v1/queue/{rid}/retry")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # Failed analyses should be cleared
    from bugalizer.db import analyses_for_report
    remaining = [a for a in analyses_for_report(rid, phase="triage") if a["status"] == "failed"]
    assert len(remaining) == 0


# ---------------------------------------------------------------------------
# Phase 7: attribution (key_source / key_ref), additive to the aggregate
# ---------------------------------------------------------------------------

def test_usage_attribution_round_trips_distinct_key_refs():
    pid = _create_project().json()["id"]
    token_usage_create(project_id=pid, provider="anthropic", model="m", prompt_tokens=10,
                       completion_tokens=1, key_source="request", key_ref="aegis:ai_settings:1")
    token_usage_create(project_id=pid, provider="anthropic", model="m", prompt_tokens=20,
                       completion_tokens=2, key_source="request", key_ref="aegis:ai_settings:2")
    token_usage_create(project_id=pid, provider="anthropic", model="m", prompt_tokens=40,
                       completion_tokens=4, key_source="request", key_ref="aegis:ai_settings:2")
    token_usage_create(project_id=pid, provider="anthropic", model="m", prompt_tokens=80,
                       completion_tokens=8, key_source="env", key_ref=None)

    for url in ("/api/v1/usage", f"/api/v1/usage/{pid}"):
        r = client.get(url)
        assert r.status_code == 200
        body = r.json()
        # Aggregate shape preserved.
        assert body["total_prompt_tokens"] == 150
        assert body["total_completion_tokens"] == 15
        assert "anthropic/m" in body["by_provider"]
        # Attribution: one entry per (key_source, key_ref).
        by_ref = {(a["key_source"], a["key_ref"]): a for a in body["attribution"]}
        assert set(by_ref) == {
            ("request", "aegis:ai_settings:1"),
            ("request", "aegis:ai_settings:2"),
            ("env", None),
        }
        assert by_ref[("request", "aegis:ai_settings:1")]["prompt_tokens"] == 10
        assert by_ref[("request", "aegis:ai_settings:1")]["calls"] == 1
        assert by_ref[("request", "aegis:ai_settings:2")]["prompt_tokens"] == 60
        assert by_ref[("request", "aegis:ai_settings:2")]["calls"] == 2
        assert by_ref[("env", None)]["prompt_tokens"] == 80


def test_usage_attribution_pre_phase7_rows_group_under_nulls():
    pid = _create_project().json()["id"]
    token_usage_create(project_id=pid, provider="ollama", model="q", prompt_tokens=5)
    body = client.get("/api/v1/usage").json()
    assert body["attribution"] == [{
        "key_source": None, "key_ref": None, "calls": 1,
        "prompt_tokens": 5, "completion_tokens": 0, "estimated_cost_usd": 0.0,
    }]


def test_usage_attribution_is_per_project():
    p1 = _create_project(name="P1").json()["id"]
    p2 = _create_project(name="P2").json()["id"]
    token_usage_create(project_id=p1, provider="anthropic", model="m", prompt_tokens=1,
                       key_source="request", key_ref="ref:one")
    token_usage_create(project_id=p2, provider="anthropic", model="m", prompt_tokens=2,
                       key_source="request", key_ref="ref:two")
    refs = {a["key_ref"] for a in client.get(f"/api/v1/usage/{p1}").json()["attribution"]}
    assert refs == {"ref:one"}
