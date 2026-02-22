"""SQLite database layer for Bugalizer."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from bugalizer.config import settings

_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript(_SCHEMA)


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
