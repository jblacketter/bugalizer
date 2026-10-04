"""Phase 12 (B4): the board lane mapping (`sync/stage.py`, plan §1).

Pure: rows are plain dicts, newest first, as db.analyses_for_report /
db.fix_proposals_for_report return them. Timestamps compare as strings.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from bugalizer.sync.stage import stage_of

HEAD = "a" * 40
OLD = "b" * 40
PROJECT = {"id": "p1", "head_sha": HEAD}


def report(status: str, **extra: Any) -> dict[str, Any]:
    return {"id": "r1", "status": status, "analysis_mode": "auto", **extra}


def analysis(phase: str, status: str, t: str, sha: Optional[str] = HEAD) -> dict[str, Any]:
    result = {"repo_sha": sha} if phase == "localization" else {}
    return {"phase": phase, "status": status, "created_at": t, "result": result}


def proposal(t: str, **extra: Any) -> dict[str, Any]:
    return {"id": "fp1", "created_at": t, "updated_at": t, **extra}


def lane(rep, analyses=(), proposals=(), project=PROJECT):
    newest_first = sorted(analyses, key=lambda a: a["created_at"], reverse=True)
    props = sorted(proposals, key=lambda p: p["created_at"], reverse=True)
    return stage_of(rep, newest_first, props, project)


@pytest.mark.parametrize("status, expected", [
    ("submitted", ("submitted", "queued")),
    ("validating", ("submitted", "validating")),
    ("fix_proposing", ("fix", "proposing")),
    ("fix_proposed", ("review", "fix_ready")),
    ("fix_approved", ("review", "opening_pr")),
    ("fix_committed", ("review", "pr_open")),
    ("rejected", ("completed", "rejected")),
    ("duplicate", ("completed", "duplicate")),
    ("closed", ("completed", "closed")),
    ("verified", ("completed", "closed")),
    ("clarification_needed", ("triaged", "needs_clarification")),
    ("deferred", ("triaged", "deferred")),
])
def test_every_status_has_a_lane(status, expected):
    assert lane(report(status)) == expected


def test_merged_completion_names_the_merge():
    assert lane(report("closed", resolution_reason="pr_merged:7")) == ("completed", "merged")
    assert lane(report("closed", resolution_reason="deleted")) == ("completed", "closed")


def test_analyzing_reports_the_running_phase():
    assert lane(report("analyzing")) == ("triage", "starting")
    assert lane(report("analyzing"), [analysis("triage", "running", "t1")]) == ("triage", "triaging")
    assert lane(report("analyzing"), [
        analysis("triage", "completed", "t1"), analysis("localization", "running", "t2"),
    ]) == ("triage", "localizing")


# --- triaged: each predicate alone -----------------------------------------

def test_triaged_alone():
    assert lane(report("triaged")) == ("triaged", "not_localized")
    assert lane(report("triaged"), [analysis("localization", "completed", "t1")]) == ("triaged", "localized")
    assert lane(report("triaged"), [analysis("localization", "completed", "t1", OLD)]) == ("triaged", "stale")
    assert lane(report("triaged", analysis_mode="hold")) == ("triaged", "held")
    assert lane(report("triaged"), [analysis("fix", "failed", "t1")]) == ("triaged", "fix_failed")
    assert lane(report("triaged"), [], [proposal("t1", pr_state="closed", pr_settled_at="t2")]) == (
        "triaged", "pr_closed")


def test_unknown_head_is_stale_like_the_fix_gate():
    assert lane(report("triaged"), [analysis("localization", "completed", "t1")],
                project={"id": "p1", "head_sha": None}) == ("triaged", "stale")


# --- triaged: overlaps (first match wins) ----------------------------------

def test_held_wins_over_localized():
    assert lane(report("triaged", analysis_mode="hold"),
                [analysis("localization", "completed", "t1")]) == ("triaged", "held")


def test_failed_fix_wins_over_localized_and_stale():
    assert lane(report("triaged"), [
        analysis("localization", "completed", "t1"), analysis("fix", "failed", "t2"),
    ]) == ("triaged", "fix_failed")
    assert lane(report("triaged"), [
        analysis("localization", "completed", "t1", OLD), analysis("fix", "failed", "t2"),
    ]) == ("triaged", "fix_failed")


def test_interrupted_fix_counts_as_failed():
    row = analysis("fix", "failed", "t2")
    row["result"] = {"interrupted": True}
    assert lane(report("triaged"), [analysis("localization", "completed", "t1"), row]) == (
        "triaged", "fix_failed")


def test_newer_localization_clears_fix_failed():
    assert lane(report("triaged"), [
        analysis("fix", "failed", "t1"), analysis("localization", "completed", "t2"),
    ]) == ("triaged", "localized")


def test_newer_proposal_clears_fix_failed():
    assert lane(report("triaged"), [
        analysis("localization", "completed", "t1"), analysis("fix", "failed", "t2"),
    ], [proposal("t3")]) == ("triaged", "localized")


def test_pr_closed_wins_over_localized():
    assert lane(report("triaged"), [analysis("localization", "completed", "t1")],
                [proposal("t2", pr_state="closed", pr_settled_at="t3")]) == ("triaged", "pr_closed")


def test_relocalization_after_a_closed_pr_clears_it():
    assert lane(report("triaged"), [analysis("localization", "completed", "t4")],
                [proposal("t2", pr_state="closed", pr_settled_at="t3")]) == ("triaged", "localized")


def test_clarification_wins_over_everything():
    assert lane(report("clarification_needed", analysis_mode="hold"),
                [analysis("fix", "failed", "t1")]) == ("triaged", "needs_clarification")
