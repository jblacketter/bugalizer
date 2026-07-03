"""Tests for queue worker and retry behavior."""

import os
import json
import sqlite3
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta

os.environ["BUGALIZER_DB_PATH"] = ":memory:"
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"

from bugalizer.db import (
    init_db,
    report_get,
    report_update_status,
    submitted_reports,
    triage_eligible_reports,
    analysis_create,
    analyses_for_report,
    reset_triage_retries,
    retry_on_locked,
)
from bugalizer.llm.client import LLMResponse


@pytest.fixture(autouse=True)
def fresh_db():
    from bugalizer import db
    db._conn = None
    os.environ["BUGALIZER_DB_PATH"] = ":memory:"
    from bugalizer.config import settings
    settings.db_path = ":memory:"
    settings.queue_enabled = False
    settings.retry_delay_seconds = 60
    settings.max_triage_retries = 3
    init_db()
    yield


def _make_project():
    from bugalizer.db import project_create
    return project_create(name="Test", repo_url="https://github.com/test/repo")


def _make_report(project_id, title="Bug", description="Something broke"):
    from bugalizer.db import report_create
    return report_create(
        project_id=project_id, title=title, description=description,
        reporter="tester@example.com",
    )


# ---------------------------------------------------------------------------
# Submitted reports query
# ---------------------------------------------------------------------------

def test_submitted_reports_returns_submitted():
    proj = _make_project()
    r1 = _make_report(proj["id"])
    r2 = _make_report(proj["id"])
    report_update_status(r2["id"], "triaged")

    results = submitted_reports()
    assert len(results) == 1
    assert results[0]["id"] == r1["id"]


# ---------------------------------------------------------------------------
# Triage eligibility
# ---------------------------------------------------------------------------

def test_triage_eligible_never_attempted():
    proj = _make_project()
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")

    eligible = triage_eligible_reports()
    assert len(eligible) == 1


def test_triage_eligible_excludes_completed():
    proj = _make_project()
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")
    analysis_create(report["id"], "triage", "completed")

    eligible = triage_eligible_reports()
    assert len(eligible) == 0


def test_triage_eligible_includes_failed_past_delay():
    proj = _make_project()
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")

    # Create a failed analysis with old timestamp
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    analysis_create(
        report["id"], "triage", "failed",
        completed_at=old_time,
    )

    eligible = triage_eligible_reports()
    assert len(eligible) == 1


def test_triage_eligible_excludes_failed_within_delay():
    proj = _make_project()
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")

    # Create a failed analysis with recent timestamp
    recent_time = datetime.now(timezone.utc).isoformat()
    analysis_create(
        report["id"], "triage", "failed",
        completed_at=recent_time,
    )

    eligible = triage_eligible_reports()
    assert len(eligible) == 0


def test_triage_eligible_excludes_max_retries():
    proj = _make_project()
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")

    # Create 3 failed analyses (max retries = 3)
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    for _ in range(3):
        analysis_create(
            report["id"], "triage", "failed",
            completed_at=old_time,
        )

    eligible = triage_eligible_reports()
    assert len(eligible) == 0


# ---------------------------------------------------------------------------
# Reset triage retries
# ---------------------------------------------------------------------------

def test_reset_triage_retries():
    proj = _make_project()
    report = _make_report(proj["id"])
    report_update_status(report["id"], "triaged")

    old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    for _ in range(3):
        analysis_create(report["id"], "triage", "failed", completed_at=old_time)

    # Max retries hit — not eligible
    assert len(triage_eligible_reports()) == 0

    # Reset
    reset_triage_retries(report["id"])

    # Now eligible again
    assert len(triage_eligible_reports()) == 1
    # Failed analyses removed
    assert len(analyses_for_report(report["id"], phase="triage")) == 0


# ---------------------------------------------------------------------------
# retry_on_locked decorator
# ---------------------------------------------------------------------------

def test_retry_on_locked_succeeds_on_first_try():
    """No OperationalError — function called once."""
    call_count = 0

    @retry_on_locked
    def fn():
        nonlocal call_count
        call_count += 1
        return "ok"

    assert fn() == "ok"
    assert call_count == 1


def test_retry_on_locked_retries_then_succeeds():
    """OperationalError on first call, succeeds on retry."""
    call_count = 0

    @retry_on_locked
    def fn():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    with patch("bugalizer.db.time.sleep"):  # Don't actually sleep in tests
        assert fn() == "ok"
    assert call_count == 2


def test_retry_on_locked_exhausts_retries():
    """OperationalError every time — raises after all retries."""
    @retry_on_locked
    def fn():
        raise sqlite3.OperationalError("database is locked")

    with patch("bugalizer.db.time.sleep"):
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            fn()


def test_retry_on_locked_ignores_other_errors():
    """Non-locked OperationalError is not retried."""
    @retry_on_locked
    def fn():
        raise sqlite3.OperationalError("no such table")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        fn()


# ---------------------------------------------------------------------------
# Stage 4 eligibility (reports_eligible_for_fix)
# ---------------------------------------------------------------------------

def test_reports_eligible_for_fix_requires_completed_localization(tmp_path):
    """Worker only picks up triaged reports with a completed localization
    analysis AND no fix proposal for that analysis yet. Reports in
    fix_proposing / fix_proposed are never re-picked up.
    """
    from bugalizer.db import (
        analysis_create,
        fix_proposal_create,
        project_create,
        project_update,
        report_create,
        reports_eligible_for_fix,
        report_update_status,
    )

    # Fresh in-memory DB (autouse fixture above already handled it)
    # Project HEAD is HEAD_SHA; fresh localizations carry the same repo_sha.
    HEAD_SHA = "abc123head"
    fresh = {"repo_sha": HEAD_SHA}
    project = project_create(name="p", repo_url="https://example.com/r.git")
    project_update(project["id"], repo_path=str(tmp_path), head_sha=HEAD_SHA)

    # Case 1: triaged, no localization -> NOT eligible
    r1 = report_create(project_id=project["id"], title="a", description="a",
                      reporter="q@e.com", severity="low")
    report_update_status(r1["id"], "triaged")

    # Case 2: triaged + completed fresh localization + no proposal -> eligible
    r2 = report_create(project_id=project["id"], title="b", description="b",
                      reporter="q@e.com", severity="low")
    report_update_status(r2["id"], "triaged")
    a2 = analysis_create(bug_report_id=r2["id"], phase="localization",
                         status="completed", result=fresh)

    # Case 3: triaged + completed localization + existing proposal -> NOT eligible
    r3 = report_create(project_id=project["id"], title="c", description="c",
                      reporter="q@e.com", severity="low")
    report_update_status(r3["id"], "triaged")
    a3 = analysis_create(bug_report_id=r3["id"], phase="localization",
                         status="completed", result=fresh)
    fix_proposal_create(
        bug_report_id=r3["id"], analysis_id=a3["id"],
        root_cause="rc", explanation="ex",
        diff="--- a/x\n+++ b/x\n@@\n-1\n+2\n",
        confidence=0.9, files_changed=["x"],
    )

    # Case 4: already in fix_proposing (claimed by another worker) -> NOT eligible
    r4 = report_create(project_id=project["id"], title="d", description="d",
                      reporter="q@e.com", severity="low")
    report_update_status(r4["id"], "triaged")
    analysis_create(bug_report_id=r4["id"], phase="localization",
                    status="completed", result=fresh)
    report_update_status(r4["id"], "fix_proposing")

    # Case 5: already in fix_proposed -> NOT eligible
    r5 = report_create(project_id=project["id"], title="e", description="e",
                      reporter="q@e.com", severity="low")
    report_update_status(r5["id"], "triaged")
    a5 = analysis_create(bug_report_id=r5["id"], phase="localization",
                         status="completed", result=fresh)
    fix_proposal_create(
        bug_report_id=r5["id"], analysis_id=a5["id"],
        root_cause="rc", explanation="ex",
        diff="--- a/x\n+++ b/x\n@@\n-1\n+2\n",
        confidence=0.9, files_changed=["x"],
    )
    report_update_status(r5["id"], "fix_proposed")

    eligible = reports_eligible_for_fix()
    eligible_ids = {row["id"] for row in eligible}
    assert r2["id"] in eligible_ids
    assert r1["id"] not in eligible_ids
    assert r3["id"] not in eligible_ids
    assert r4["id"] not in eligible_ids
    assert r5["id"] not in eligible_ids


def test_reports_eligible_for_fix_excludes_stale_localization(tmp_path):
    """A completed localization whose repo_sha != project.head_sha is stale
    and must NOT be eligible for Stage 4 — otherwise the paid cloud fix
    model runs on out-of-date file evidence and races Stage 3
    re-localization. Covers three stale shapes: SHA mismatch, missing
    repo_sha, and project with no known head_sha.
    """
    from bugalizer.db import (
        analysis_create,
        localization_eligible_reports,
        project_create,
        project_update,
        report_create,
        reports_eligible_for_fix,
        report_update_status,
    )

    HEAD_SHA = "newhead999"
    project = project_create(name="p", repo_url="https://example.com/r.git")
    project_update(project["id"], repo_path=str(tmp_path), head_sha=HEAD_SHA)

    # Every report has a completed triage (Stage 3 re-localization requires it).
    # Stale: localization's repo_sha is an older commit than project HEAD.
    stale = report_create(project_id=project["id"], title="stale", description="d",
                          reporter="q@e.com", severity="low")
    report_update_status(stale["id"], "triaged")
    analysis_create(bug_report_id=stale["id"], phase="triage", status="completed")
    analysis_create(bug_report_id=stale["id"], phase="localization",
                    status="completed", result={"repo_sha": "oldsha000"})

    # Stale: localization result has no repo_sha at all (pre-freshness data).
    no_sha = report_create(project_id=project["id"], title="nosha", description="d",
                           reporter="q@e.com", severity="low")
    report_update_status(no_sha["id"], "triaged")
    analysis_create(bug_report_id=no_sha["id"], phase="triage", status="completed")
    analysis_create(bug_report_id=no_sha["id"], phase="localization",
                    status="completed", result={"x": 1})

    # Fresh control: repo_sha matches project HEAD -> eligible.
    fresh = report_create(project_id=project["id"], title="fresh", description="d",
                          reporter="q@e.com", severity="low")
    report_update_status(fresh["id"], "triaged")
    analysis_create(bug_report_id=fresh["id"], phase="triage", status="completed")
    analysis_create(bug_report_id=fresh["id"], phase="localization",
                    status="completed", result={"repo_sha": HEAD_SHA})

    eligible_ids = {row["id"] for row in reports_eligible_for_fix()}
    assert fresh["id"] in eligible_ids
    assert stale["id"] not in eligible_ids
    assert no_sha["id"] not in eligible_ids

    # The stale reports are instead picked up by Stage 3 for re-localization,
    # so they never race into Stage 4 with out-of-date evidence.
    reloc_ids = {row["id"] for row in localization_eligible_reports()}
    assert stale["id"] in reloc_ids
    assert no_sha["id"] in reloc_ids
    assert fresh["id"] not in reloc_ids


def test_reports_eligible_for_fix_excludes_when_project_head_unknown(tmp_path):
    """If the project has no known head_sha, freshness cannot be confirmed,
    so no report is eligible for the paid Stage 4 fix model."""
    from bugalizer.db import (
        analysis_create,
        project_create,
        project_update,
        report_create,
        reports_eligible_for_fix,
        report_update_status,
    )

    project = project_create(name="p", repo_url="https://example.com/r.git")
    project_update(project["id"], repo_path=str(tmp_path))  # head_sha stays NULL

    r = report_create(project_id=project["id"], title="a", description="d",
                      reporter="q@e.com", severity="low")
    report_update_status(r["id"], "triaged")
    analysis_create(bug_report_id=r["id"], phase="localization",
                    status="completed", result={"repo_sha": "anysha"})

    assert reports_eligible_for_fix() == []


# ---------------------------------------------------------------------------
# Stage 3 / Stage 4 retry caps + failure surfacing (Phase 5.1)
# ---------------------------------------------------------------------------

def _seed_localization_project(tmp_path):
    from bugalizer.db import project_create, project_update
    project = project_create(name="p", repo_url="https://example.com/r.git")
    project_update(project["id"], repo_path=str(tmp_path), head_sha="h1")
    return project


def test_localization_retry_cap_and_reset(tmp_path):
    from bugalizer.config import settings
    from bugalizer.db import (
        localization_eligible_reports, report_create,
        report_update_status, reset_stage_retries,
    )
    settings.max_localize_retries = 3
    settings.retry_delay_seconds = 60

    project = _seed_localization_project(tmp_path)
    r = report_create(project_id=project["id"], title="t", description="d",
                      reporter="q@e.com", severity="low")
    report_update_status(r["id"], "triaged")
    analysis_create(r["id"], "triage", "completed")

    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()

    def eligible_ids():
        return {x["id"] for x in localization_eligible_reports()}

    # 2 failures (< cap of 3): still eligible (never successfully localized).
    for _ in range(2):
        analysis_create(r["id"], "localization", "failed",
                        result={"error": "boom"}, completed_at=old)
    assert r["id"] in eligible_ids()

    # 3rd failure reaches the cap: excluded from dispatch.
    analysis_create(r["id"], "localization", "failed",
                    result={"error": "boom"}, completed_at=old)
    assert r["id"] not in eligible_ids()

    # Retry endpoint clears failed rows -> eligible again.
    reset_stage_retries(r["id"])
    assert r["id"] in eligible_ids()


def test_localization_retry_delay_blocks_recent_failure(tmp_path):
    from bugalizer.config import settings
    from bugalizer.db import (
        localization_eligible_reports, report_create, report_update_status,
    )
    settings.max_localize_retries = 3
    settings.retry_delay_seconds = 60

    project = _seed_localization_project(tmp_path)
    r = report_create(project_id=project["id"], title="t", description="d",
                      reporter="q@e.com", severity="low")
    report_update_status(r["id"], "triaged")
    analysis_create(r["id"], "triage", "completed")

    # A single *recent* failure is within the retry-delay window -> not eligible.
    recent = datetime.now(timezone.utc).isoformat()
    analysis_create(r["id"], "localization", "failed",
                    result={"error": "boom"}, completed_at=recent)
    assert r["id"] not in {x["id"] for x in localization_eligible_reports()}


def test_fix_retry_cap_transient_and_permanent(tmp_path):
    from bugalizer.config import settings
    from bugalizer.db import (
        reports_eligible_for_fix, report_create, report_update_status,
        reset_stage_retries,
    )
    settings.max_fix_retries = 2
    settings.retry_delay_seconds = 60

    project = _seed_localization_project(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()

    def fresh_report():
        rr = report_create(project_id=project["id"], title="t", description="d",
                           reporter="q@e.com", severity="low")
        report_update_status(rr["id"], "triaged")
        analysis_create(rr["id"], "localization", "completed",
                        result={"repo_sha": "h1"})
        return rr

    def eligible_ids():
        return {x["id"] for x in reports_eligible_for_fix()}

    # Transient: 1 failure (< cap 2) -> still eligible.
    r1 = fresh_report()
    analysis_create(r1["id"], "fix", "failed",
                    result={"error": "timeout", "permanent": False}, completed_at=old)
    assert r1["id"] in eligible_ids()
    # 2nd transient failure reaches the (low) paid-call cap -> excluded.
    analysis_create(r1["id"], "fix", "failed",
                    result={"error": "timeout", "permanent": False}, completed_at=old)
    assert r1["id"] not in eligible_ids()
    reset_stage_retries(r1["id"])
    assert r1["id"] in eligible_ids()

    # Permanent: a single permanent failure excludes immediately (no retry).
    r2 = fresh_report()
    analysis_create(r2["id"], "fix", "failed",
                    result={"error": "bad diff", "permanent": True}, completed_at=old)
    assert r2["id"] not in eligible_ids()


def test_report_failure_info_reports_latest_failed_stage(tmp_path):
    from bugalizer.db import (
        report_failure_info, report_create, report_update_status,
    )
    project = _seed_localization_project(tmp_path)
    r = report_create(project_id=project["id"], title="t", description="d",
                      reporter="q@e.com", severity="low")
    report_update_status(r["id"], "triaged")

    # No failures yet.
    assert report_failure_info(r["id"]) is None

    analysis_create(r["id"], "localization", "failed",
                    result={"error": "loc boom", "permanent": False})
    info = report_failure_info(r["id"])
    assert info["failed_stage"] == "localization"
    assert info["last_error"] == "loc boom"
    assert info["permanent"] is False

    # A later completed localization clears the failure from the report view.
    analysis_create(r["id"], "localization", "completed", result={"repo_sha": "h1"})
    assert report_failure_info(r["id"]) is None
