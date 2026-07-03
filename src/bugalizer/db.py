"""SQLite database layer for Bugalizer."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

from bugalizer.config import settings

logger = logging.getLogger(__name__)

_conn: Optional[sqlite3.Connection] = None

# Async lock for serializing DB writes from queue workers.
db_write_lock = asyncio.Lock()

T = TypeVar("T")


def retry_on_locked(fn: Callable[..., T]) -> Callable[..., T]:
    """Decorator: retry a DB function up to 3 times on sqlite3.OperationalError.

    Uses exponential backoff: 0.1s, 0.2s, 0.4s.
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        delays = [0.1, 0.2, 0.4]
        for attempt in range(len(delays) + 1):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as e:
                if "database is locked" not in str(e) or attempt == len(delays):
                    raise
                delay = delays[attempt]
                logger.warning(
                    "DB locked in %s (attempt %d/%d), retrying in %.1fs",
                    fn.__name__, attempt + 1, len(delays) + 1, delay,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover
    return wrapper


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db() -> None:
    """Create tables if they don't exist, then apply any pending migrations."""
    conn = _get_conn()
    conn.executescript(_SCHEMA)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply lightweight schema migrations for columns added after initial release."""
    # Phase 3: projects.head_sha (added for localization freshness tracking)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "head_sha" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN head_sha TEXT")
        conn.commit()
        logger.info("Migration: added projects.head_sha column")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    repo_path TEXT,
    head_sha TEXT,
    default_branch TEXT DEFAULT 'main',
    llm_provider TEXT DEFAULT 'ollama',
    llm_model TEXT DEFAULT 'qwen2.5-coder:7b',
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

CREATE INDEX IF NOT EXISTS idx_bug_reports_project ON bug_reports(project_id);
CREATE INDEX IF NOT EXISTS idx_bug_reports_status ON bug_reports(status);

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

CREATE INDEX IF NOT EXISTS idx_analyses_bug_report ON analyses(bug_report_id);

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

CREATE INDEX IF NOT EXISTS idx_fix_proposals_bug_report ON fix_proposals(bug_report_id);

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

CREATE INDEX IF NOT EXISTS idx_token_usage_project ON token_usage(project_id);
"""


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def project_create(
    name: str,
    repo_url: str,
    default_branch: str = "main",
    llm_provider: str = "ollama",
    llm_model: str = "qwen2.5-coder:7b",
) -> dict[str, Any]:
    conn = _get_conn()
    row_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO projects (id, name, repo_url, default_branch, llm_provider, llm_model, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (row_id, name, repo_url, default_branch, llm_provider, llm_model, now, now),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM projects WHERE id = ?", (row_id,)).fetchone())


def project_get(project_id: str) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def project_list() -> list[dict[str, Any]]:
    conn = _get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()]


def project_update(project_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    existing = project_get(project_id)
    if not existing:
        return None
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [project_id]
    conn.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
    conn.commit()
    return project_get(project_id)


def project_has_active_reports(project_id: str) -> bool:
    """Return True if any non-deleted bug reports reference this project."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM bug_reports WHERE project_id = ? AND (resolution_reason IS NULL OR resolution_reason != 'deleted') LIMIT 1",
        (project_id,),
    ).fetchone()
    return row is not None


def project_delete(project_id: str) -> bool | str:
    """Delete a project. Returns True on success, False if not found,
    or the string 'has_reports' if FK constraint would fail."""
    conn = _get_conn()
    if not project_exists(project_id):
        return False
    if project_has_active_reports(project_id):
        return "has_reports"
    # Clean up soft-deleted reports before removing the project (FK constraint).
    conn.execute(
        "DELETE FROM bug_reports WHERE project_id = ? AND resolution_reason = 'deleted'",
        (project_id,),
    )
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    return True


def project_exists(project_id: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Bug reports
# ---------------------------------------------------------------------------

def _serialize_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value)


def _deserialize_json(value: Optional[str]) -> Any:
    if value is None:
        return None
    return json.loads(value)


def report_create(
    project_id: str,
    title: str,
    description: str,
    reporter: str,
    *,
    steps_to_reproduce: Optional[list[str]] = None,
    expected_behavior: Optional[str] = None,
    actual_behavior: Optional[str] = None,
    url: Optional[str] = None,
    feature_area: Optional[str] = None,
    severity: str = "medium",
    environment: Optional[str] = None,
    labels: Optional[list[str]] = None,
) -> dict[str, Any]:
    conn = _get_conn()
    row_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO bug_reports
           (id, project_id, title, description, reporter,
            steps_to_reproduce, expected_behavior, actual_behavior,
            url, feature_area, severity, environment, labels,
            status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?)""",
        (
            row_id, project_id, title, description, reporter,
            _serialize_json(steps_to_reproduce), expected_behavior, actual_behavior,
            url, feature_area, severity, environment, _serialize_json(labels),
            now, now,
        ),
    )
    conn.commit()
    return _report_row_to_dict(
        conn.execute("SELECT * FROM bug_reports WHERE id = ?", (row_id,)).fetchone()
    )


def _report_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["steps_to_reproduce"] = _deserialize_json(d.get("steps_to_reproduce"))
    d["labels"] = _deserialize_json(d.get("labels"))
    d["attachments"] = _deserialize_json(d.get("attachments"))
    return d


def report_get(report_id: str) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM bug_reports WHERE id = ?", (report_id,)).fetchone()
    return _report_row_to_dict(row) if row else None


def report_list(
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    conn = _get_conn()
    query = "SELECT * FROM bug_reports WHERE 1=1"
    params: list[Any] = []
    if not include_deleted:
        query += " AND (resolution_reason IS NULL OR resolution_reason != 'deleted')"
    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    return [_report_row_to_dict(r) for r in conn.execute(query, params).fetchall()]


@retry_on_locked
def report_update_status(
    report_id: str,
    new_status: str,
    resolution_reason: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    now = _now()
    fields = {"status": new_status, "updated_at": now}
    if resolution_reason is not None:
        fields["resolution_reason"] = resolution_reason
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [report_id]
    conn.execute(f"UPDATE bug_reports SET {set_clause} WHERE id = ?", values)
    conn.commit()
    return report_get(report_id)


def report_delete(report_id: str) -> bool:
    """Soft-delete a report by setting status to 'rejected' with resolution_reason 'deleted'."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM bug_reports WHERE id = ? AND (resolution_reason IS NULL OR resolution_reason != 'deleted')",
        (report_id,),
    ).fetchone()
    if not row:
        return False
    now = _now()
    conn.execute(
        "UPDATE bug_reports SET status = 'rejected', resolution_reason = 'deleted', updated_at = ? WHERE id = ?",
        (now, report_id),
    )
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Queue overview
# ---------------------------------------------------------------------------

def queue_counts(project_id: Optional[str] = None) -> dict[str, int]:
    conn = _get_conn()
    query = "SELECT status, COUNT(*) as cnt FROM bug_reports WHERE (resolution_reason IS NULL OR resolution_reason != 'deleted')"
    params: list[Any] = []
    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)
    query += " GROUP BY status"
    rows = conn.execute(query, params).fetchall()
    return {row["status"]: row["cnt"] for row in rows}


# ---------------------------------------------------------------------------
# Atomic claim for queue workers
# ---------------------------------------------------------------------------

@retry_on_locked
def try_claim_report(report_id: str, expected_status: str, new_status: str) -> bool:
    """Atomically claim a report by transitioning its status.

    Returns True only if this caller won the claim (rowcount == 1).
    """
    conn = _get_conn()
    now = _now()
    cursor = conn.execute(
        "UPDATE bug_reports SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
        (new_status, now, report_id, expected_status),
    )
    conn.commit()
    return cursor.rowcount == 1


@retry_on_locked
def report_update_fields(report_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    """Update arbitrary fields on a bug report."""
    conn = _get_conn()
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [report_id]
    conn.execute(f"UPDATE bug_reports SET {set_clause} WHERE id = ?", values)
    conn.commit()
    return report_get(report_id)


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

@retry_on_locked
def analysis_create(
    bug_report_id: str,
    phase: str,
    status: str = "pending",
    *,
    result: Optional[dict] = None,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> dict[str, Any]:
    conn = _get_conn()
    row_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO analyses
           (id, bug_report_id, phase, status, result,
            llm_provider, llm_model, prompt_tokens, completion_tokens,
            estimated_cost_usd, started_at, completed_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row_id, bug_report_id, phase, status,
            json.dumps(result) if result else None,
            llm_provider, llm_model, prompt_tokens, completion_tokens,
            estimated_cost_usd, started_at, completed_at, now,
        ),
    )
    conn.commit()
    return _analysis_row_to_dict(
        conn.execute("SELECT * FROM analyses WHERE id = ?", (row_id,)).fetchone()
    )


def _analysis_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("result"):
        d["result"] = json.loads(d["result"])
    return d


@retry_on_locked
def analysis_update(analysis_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    if "result" in fields and isinstance(fields["result"], dict):
        fields["result"] = json.dumps(fields["result"])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [analysis_id]
    conn.execute(f"UPDATE analyses SET {set_clause} WHERE id = ?", values)
    conn.commit()
    row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    return _analysis_row_to_dict(row) if row else None


def analysis_get(analysis_id: str) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    return _analysis_row_to_dict(row) if row else None


def analyses_for_report(bug_report_id: str, phase: Optional[str] = None) -> list[dict[str, Any]]:
    conn = _get_conn()
    query = "SELECT * FROM analyses WHERE bug_report_id = ?"
    params: list[Any] = [bug_report_id]
    if phase:
        query += " AND phase = ?"
        params.append(phase)
    query += " ORDER BY created_at DESC"
    return [_analysis_row_to_dict(r) for r in conn.execute(query, params).fetchall()]


def triage_eligible_reports() -> list[dict[str, Any]]:
    """Return triaged reports eligible for Stage 2 triage processing.

    Eligible when:
    - No triage analysis with status='completed' exists
    - Either no triage analysis at all, or most recent failed triage is
      past the retry delay and retry count < max retries
    """
    conn = _get_conn()
    max_retries = settings.max_triage_retries
    retry_delay = settings.retry_delay_seconds

    # Get all triaged, non-deleted reports
    reports = [
        _report_row_to_dict(r) for r in conn.execute(
            """SELECT * FROM bug_reports
               WHERE status = 'triaged'
               AND (resolution_reason IS NULL OR resolution_reason != 'deleted')
               ORDER BY created_at ASC"""
        ).fetchall()
    ]

    eligible = []
    now = datetime.now(timezone.utc)
    for report in reports:
        triage_rows = conn.execute(
            """SELECT status, completed_at FROM analyses
               WHERE bug_report_id = ? AND phase = 'triage'
               ORDER BY created_at DESC""",
            (report["id"],),
        ).fetchall()

        if not triage_rows:
            # Never attempted — eligible
            eligible.append(report)
            continue

        # Check if any completed successfully
        if any(r["status"] == "completed" for r in triage_rows):
            continue  # Already triaged

        # Count failed attempts
        failed_count = sum(1 for r in triage_rows if r["status"] == "failed")
        if failed_count >= max_retries:
            continue  # Max retries exceeded

        # Check retry delay on most recent failure
        latest = triage_rows[0]
        if latest["status"] == "failed" and latest["completed_at"]:
            completed = datetime.fromisoformat(latest["completed_at"])
            elapsed = (now - completed).total_seconds()
            if elapsed < retry_delay:
                continue  # Within retry delay window

        eligible.append(report)

    return eligible


def submitted_reports() -> list[dict[str, Any]]:
    """Return reports in 'submitted' status ready for Stage 1."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM bug_reports
           WHERE status = 'submitted'
           AND (resolution_reason IS NULL OR resolution_reason != 'deleted')
           ORDER BY created_at ASC"""
    ).fetchall()
    return [_report_row_to_dict(r) for r in rows]


def _latest_completed_created_at(rows: list[dict[str, Any]]) -> Optional[str]:
    """Given analysis dicts (newest-first), return the newest completed row's
    `created_at`, or None if none completed."""
    for r in rows:
        if r.get("status") == "completed":
            return r.get("created_at")
    return None


def _failed_attempts_after(
    rows: list[dict[str, Any]], reference_created_at: Optional[str]
) -> list[dict[str, Any]]:
    """Failed analysis rows (newest-first order preserved) that are newer than
    `reference_created_at`. A successful attempt resets the failure budget, so
    only failures since the last success count. ISO-8601 UTC timestamps compare
    lexicographically."""
    out = []
    for r in rows:
        if r.get("status") != "failed":
            continue
        created = r.get("created_at")
        if reference_created_at is None or (created and created > reference_created_at):
            out.append(r)
    return out


def _retry_blocked(
    failed_rows: list[dict[str, Any]],
    max_retries: int,
    retry_delay: int,
    now: datetime,
) -> bool:
    """Return True if a stage should be SKIPPED given its failed attempts.

    Blocks when: any failure is marked `permanent` (never retry), the failure
    count has reached `max_retries`, or the most recent failure is still within
    the `retry_delay` window. `failed_rows` must be newest-first.
    """
    if not failed_rows:
        return False
    for r in failed_rows:
        result = r.get("result")
        if isinstance(result, dict) and result.get("permanent"):
            return True
    if len(failed_rows) >= max_retries:
        return True
    latest = failed_rows[0]
    completed_at = latest.get("completed_at")
    if completed_at:
        try:
            elapsed = (now - datetime.fromisoformat(completed_at)).total_seconds()
        except ValueError:
            return False
        if elapsed < retry_delay:
            return True
    return False


def localization_eligible_reports() -> list[dict[str, Any]]:
    """Return triaged reports eligible for Stage 3 localization.

    Eligible when:
    - Report is triaged
    - Has a completed triage analysis
    - Project has repo_path set (repo cloned)
    - Either no completed localization analysis, or latest completed
      localization's repo_sha differs from project.head_sha
    - Not blocked by the localization retry gate (max_localize_retries /
      retry_delay_seconds / a permanent failure), derived from failed
      localization analysis rows since the last successful localization.
    """
    conn = _get_conn()
    now = datetime.now(timezone.utc)

    # Get triaged reports with completed triage and project with repo_path
    rows = conn.execute(
        """SELECT br.*, p.repo_path, p.default_branch, p.head_sha AS project_head_sha
           FROM bug_reports br
           JOIN projects p ON br.project_id = p.id
           WHERE br.status = 'triaged'
           AND p.repo_path IS NOT NULL
           AND (br.resolution_reason IS NULL OR br.resolution_reason != 'deleted')
           AND EXISTS (
               SELECT 1 FROM analyses a
               WHERE a.bug_report_id = br.id
               AND a.phase = 'triage' AND a.status = 'completed'
           )
           ORDER BY br.created_at ASC"""
    ).fetchall()

    eligible = []
    for row in rows:
        report = _report_row_to_dict(row)
        report["_repo_path"] = row["repo_path"]
        report["_default_branch"] = row["default_branch"]
        project_head_sha = row["project_head_sha"]

        # Retry gate: skip reports whose localization keeps failing. Failures
        # since the last successful localization count toward the cap.
        all_loc = analyses_for_report(report["id"], phase="localization")
        failed = _failed_attempts_after(all_loc, _latest_completed_created_at(all_loc))
        if _retry_blocked(failed, settings.max_localize_retries,
                          settings.retry_delay_seconds, now):
            continue

        # Check localization state
        loc_rows = conn.execute(
            """SELECT result FROM analyses
               WHERE bug_report_id = ? AND phase = 'localization' AND status = 'completed'
               ORDER BY created_at DESC LIMIT 1""",
            (report["id"],),
        ).fetchall()

        if not loc_rows:
            # Never localized — eligible
            eligible.append(report)
            continue

        # Has completed localization — compare repo_sha against project.head_sha
        if not project_head_sha:
            # Project has no known HEAD SHA yet — skip (will be set on next clone/refresh)
            continue

        try:
            result_json = loc_rows[0]["result"]
            if result_json:
                result = json.loads(result_json)
                loc_sha = result.get("repo_sha")
                if loc_sha == project_head_sha:
                    # Localization is fresh — skip
                    continue
                # SHA differs — stale localization, re-eligible
                eligible.append(report)
            else:
                eligible.append(report)
        except (json.JSONDecodeError, KeyError):
            eligible.append(report)

    return eligible


# ---------------------------------------------------------------------------
# Fix proposals (Stage 4 / bugalizer Phase 4)
# ---------------------------------------------------------------------------

@retry_on_locked
def fix_proposal_create(
    *,
    bug_report_id: str,
    analysis_id: Optional[str],
    root_cause: str,
    explanation: str,
    diff: str,
    confidence: float,
    files_changed: list[str],
) -> dict[str, Any]:
    """Insert a new fix_proposals row and return the created record."""
    conn = _get_conn()
    row_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO fix_proposals
           (id, bug_report_id, analysis_id, branch_name, diff, explanation,
            confidence, root_cause, files_changed, status, created_at, updated_at)
           VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'proposed', ?, ?)""",
        (row_id, bug_report_id, analysis_id, diff, explanation,
         confidence, root_cause, json.dumps(files_changed), now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM fix_proposals WHERE id = ?", (row_id,)
    ).fetchone()
    return _fix_proposal_row_to_dict(row)


def _fix_proposal_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("files_changed"):
        try:
            d["files_changed"] = json.loads(d["files_changed"])
        except json.JSONDecodeError:
            pass
    return d


def fix_proposals_for_report(bug_report_id: str) -> list[dict[str, Any]]:
    """Return all fix_proposals rows for a report, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM fix_proposals
           WHERE bug_report_id = ?
           ORDER BY created_at DESC""",
        (bug_report_id,),
    ).fetchall()
    return [_fix_proposal_row_to_dict(r) for r in rows]


def reports_eligible_for_fix() -> list[dict[str, Any]]:
    """Return reports eligible for Stage 4 (fix proposal generation).

    Eligible when:
    - Report is in status `triaged`.
    - Has at least one completed localization analysis.
    - The latest completed localization is *fresh*: its `result.repo_sha`
      matches the project's `head_sha`. Stale localization is excluded so
      the paid cloud fix model never runs on out-of-date file evidence and
      never races Stage 3 re-localization (see
      `docs/phases/phase-4-fix-proposals.md`).
    - Does NOT yet have a fix_proposals row for that latest localization
      analysis.
    - Not blocked by the fix retry gate (max_fix_retries / retry_delay_seconds /
      a permanent failure), derived from failed `fix` analysis rows recorded
      since the current localization. Each retry is a paid cloud call, so the
      cap is deliberately low and permanent failures never retry.
    - Not soft-deleted.
    """
    conn = _get_conn()
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT br.*, p.head_sha AS project_head_sha
           FROM bug_reports br
           JOIN projects p ON br.project_id = p.id
           WHERE br.status = 'triaged'
           AND (br.resolution_reason IS NULL OR br.resolution_reason != 'deleted')
           AND EXISTS (
               SELECT 1 FROM analyses a
               WHERE a.bug_report_id = br.id
               AND a.phase = 'localization' AND a.status = 'completed'
           )
           ORDER BY br.created_at ASC"""
    ).fetchall()

    eligible: list[dict[str, Any]] = []
    for row in rows:
        report = _report_row_to_dict(row)
        project_head_sha = row["project_head_sha"]

        # Latest completed localization for this report.
        loc = conn.execute(
            """SELECT id, result, created_at FROM analyses
               WHERE bug_report_id = ?
               AND phase = 'localization' AND status = 'completed'
               ORDER BY created_at DESC LIMIT 1""",
            (report["id"],),
        ).fetchone()
        if loc is None:
            continue

        # Freshness gate: require a known project HEAD and a matching
        # localization repo_sha. Missing/unparseable SHA is treated as stale.
        if not project_head_sha:
            continue
        try:
            result = json.loads(loc["result"]) if loc["result"] else {}
        except json.JSONDecodeError:
            continue
        if not isinstance(result, dict) or result.get("repo_sha") != project_head_sha:
            continue

        # Skip if a proposal already exists for this exact localization.
        already = conn.execute(
            "SELECT 1 FROM fix_proposals WHERE bug_report_id = ? AND analysis_id = ? LIMIT 1",
            (report["id"], loc["id"]),
        ).fetchone()
        if already is not None:
            continue

        # Retry gate: failed `fix` attempts since the current localization was
        # produced count toward the cap; a fresh localization resets the budget.
        fix_rows = analyses_for_report(report["id"], phase="fix")
        failed = _failed_attempts_after(fix_rows, loc["created_at"])
        if _retry_blocked(failed, settings.max_fix_retries,
                          settings.retry_delay_seconds, now):
            continue

        eligible.append(report)

    return eligible


def latest_completed_localization(bug_report_id: str) -> Optional[dict[str, Any]]:
    """Return the newest completed localization analysis for a report, or None."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT * FROM analyses
           WHERE bug_report_id = ?
           AND phase = 'localization' AND status = 'completed'
           ORDER BY created_at DESC LIMIT 1""",
        (bug_report_id,),
    ).fetchone()
    return _analysis_row_to_dict(row) if row else None


def reset_triage_retries(bug_report_id: str) -> bool:
    """Delete failed triage analyses for a report, making it eligible for retry."""
    conn = _get_conn()
    cursor = conn.execute(
        "DELETE FROM analyses WHERE bug_report_id = ? AND phase = 'triage' AND status = 'failed'",
        (bug_report_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


# Pipeline stages that accumulate failed analysis rows and are gated by a
# retry cap. `validation` is excluded — it never retries via this path.
_RETRYABLE_PHASES = ("triage", "localization", "fix")


@retry_on_locked
def reset_stage_retries(bug_report_id: str) -> bool:
    """Delete failed triage/localization/fix analyses for a report so the worker
    re-dispatches it. Returns True if any failed row was removed."""
    conn = _get_conn()
    placeholders = ",".join("?" for _ in _RETRYABLE_PHASES)
    cursor = conn.execute(
        f"DELETE FROM analyses WHERE bug_report_id = ? AND status = 'failed' "
        f"AND phase IN ({placeholders})",
        (bug_report_id, *_RETRYABLE_PHASES),
    )
    conn.commit()
    return cursor.rowcount > 0


def report_failure_info(bug_report_id: str) -> Optional[dict[str, Any]]:
    """Return `{failed_stage, last_error, permanent}` for the most recent failed
    pipeline analysis of a report, or None if there is no failure on record.

    A stage that later completed successfully is not reported: only failures
    newer than that stage's last success count (mirrors the retry gate).
    """
    stage_names = {"triage": "triage", "localization": "localization", "fix": "fix"}
    latest_failure: Optional[dict[str, Any]] = None
    for phase in _RETRYABLE_PHASES:
        rows = analyses_for_report(bug_report_id, phase=phase)
        failed = _failed_attempts_after(rows, _latest_completed_created_at(rows))
        if not failed:
            continue
        candidate = failed[0]  # newest-first
        if latest_failure is None or (
            candidate.get("created_at", "") > latest_failure.get("created_at", "")
        ):
            latest_failure = {"phase": phase, **candidate}
    if latest_failure is None:
        return None
    result = latest_failure.get("result")
    error = None
    permanent = False
    if isinstance(result, dict):
        error = result.get("error")
        permanent = bool(result.get("permanent"))
    return {
        "failed_stage": stage_names.get(latest_failure["phase"], latest_failure["phase"]),
        "last_error": error,
        "permanent": permanent,
    }


# ---------------------------------------------------------------------------
# Token usage
# ---------------------------------------------------------------------------

@retry_on_locked
def token_usage_create(
    project_id: str,
    provider: str,
    model: str,
    *,
    bug_report_id: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
) -> dict[str, Any]:
    conn = _get_conn()
    now = _now()
    conn.execute(
        """INSERT INTO token_usage
           (project_id, bug_report_id, provider, model,
            prompt_tokens, completion_tokens, estimated_cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, bug_report_id, provider, model,
         prompt_tokens, completion_tokens, estimated_cost_usd, now),
    )
    conn.commit()
    return {
        "project_id": project_id,
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }


def token_usage_summary(project_id: Optional[str] = None) -> dict[str, Any]:
    """Aggregate token usage, optionally filtered by project."""
    conn = _get_conn()
    query = """SELECT provider, model,
               SUM(prompt_tokens) as total_prompt,
               SUM(completion_tokens) as total_completion,
               SUM(estimated_cost_usd) as total_cost
               FROM token_usage"""
    params: list[Any] = []
    if project_id:
        query += " WHERE project_id = ?"
        params.append(project_id)
    query += " GROUP BY provider, model"
    rows = conn.execute(query, params).fetchall()

    total_prompt = 0
    total_completion = 0
    total_cost = 0.0
    by_provider: dict[str, dict] = {}

    for row in rows:
        total_prompt += row["total_prompt"]
        total_completion += row["total_completion"]
        total_cost += row["total_cost"]
        key = f"{row['provider']}/{row['model']}"
        by_provider[key] = {
            "prompt_tokens": row["total_prompt"],
            "completion_tokens": row["total_completion"],
            "estimated_cost_usd": row["total_cost"],
        }

    return {
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_estimated_cost_usd": total_cost,
        "by_provider": by_provider,
    }
