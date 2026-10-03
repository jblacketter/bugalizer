"""SQLite database layer for Bugalizer."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence, TypeVar

from bugalizer.config import settings

logger = logging.getLogger(__name__)

_conn: Optional[sqlite3.Connection] = None   # shared conn — :memory: DBs only (tests)
_local = threading.local()                   # per-thread conns for file DBs
_generation = 0                              # bumped by reset_conn() to invalidate all

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


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def reset_conn() -> None:
    """Invalidate every cached connection (tests swap BUGALIZER_DB_PATH).

    Bumps the generation so each thread lazily discards its cached connection
    on next use — connections owned by other threads must not be closed from
    here (that's itself a cross-thread use).
    """
    global _conn, _generation
    _generation += 1
    if _conn is not None:
        _conn.close()
        _conn = None
    cached = getattr(_local, "conn", None)
    if cached is not None:
        cached.close()
        _local.conn = None


def _get_conn() -> sqlite3.Connection:
    """Connection for the current thread.

    A sqlite3.Connection must never be used by two threads at once: CPython's
    sqlite3 can run with threadsafety=1 (e.g. macOS system libsqlite3), where
    concurrent statements on a shared connection corrupt the heap — the
    dashboard's parallel polls through FastAPI's threadpool did exactly that
    (SIGSEGV in sqlite3Prepare / "database disk image is malformed").

    File DBs therefore get one connection per thread (cheap under WAL; write
    contention is covered by the busy timeout + retry_on_locked). `:memory:`
    DBs (tests) can't be shared across connections, so they keep the single
    shared connection — safe there because TestClient serializes requests.
    """
    global _conn
    if settings.db_path == ":memory:":
        if _conn is None:
            _conn = _configure(sqlite3.connect(settings.db_path, check_same_thread=False))
        return _conn
    cached = getattr(_local, "conn", None)
    if cached is None or getattr(_local, "generation", None) != _generation:
        if cached is not None:
            cached.close()
        _local.conn = _configure(sqlite3.connect(settings.db_path, timeout=10))
        _local.generation = _generation
    return _local.conn


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

    # Phase 5 (§5.3): per-project Stage 4 fix model override. Nullable —
    # NULL means "use the global fix_provider / default_fix_model". A separate
    # namespace from llm_provider/llm_model, which scope local stages only.
    if "fix_llm_provider" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN fix_llm_provider TEXT")
        conn.commit()
        logger.info("Migration: added projects.fix_llm_provider column")
    if "fix_llm_model" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN fix_llm_model TEXT")
        conn.commit()
        logger.info("Migration: added projects.fix_llm_model column")

    # Phase 5 (§5.3): bug_reports.analysis_mode — per-report tier selection
    # (auto | local_only | hold). Existing rows get 'auto' (today's behavior).
    br_columns = {row[1] for row in conn.execute("PRAGMA table_info(bug_reports)").fetchall()}
    if "analysis_mode" not in br_columns:
        conn.execute(
            "ALTER TABLE bug_reports ADD COLUMN analysis_mode TEXT NOT NULL DEFAULT 'auto'"
        )
        conn.commit()
        logger.info("Migration: added bug_reports.analysis_mode column")

    # Phase 7: per-project ingest seam (B1 builds the poller on it). Both
    # nullable; the API enforces "both null or both set".
    if "ingest_source" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN ingest_source TEXT")
        conn.commit()
        logger.info("Migration: added projects.ingest_source column")
    if "ingest_config" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN ingest_config TEXT")
        conn.commit()
        logger.info("Migration: added projects.ingest_config column")

    # Phase 7: cloud-spend attribution. key_source = request | env; key_ref =
    # the caller's opaque settings-row reference. Pre-Phase-7 rows stay null.
    # (An empty column set means the table does not exist yet; the schema
    # script creates it with both columns, so there is nothing to alter.)
    tu_columns = {row[1] for row in conn.execute("PRAGMA table_info(token_usage)").fetchall()}
    if tu_columns and "key_source" not in tu_columns:
        conn.execute("ALTER TABLE token_usage ADD COLUMN key_source TEXT")
        conn.commit()
        logger.info("Migration: added token_usage.key_source column")
    if tu_columns and "key_ref" not in tu_columns:
        conn.execute("ALTER TABLE token_usage ADD COLUMN key_ref TEXT")
        conn.commit()
        logger.info("Migration: added token_usage.key_ref column")

    # Phase 8 (open-pr): the claim owner token on reports, and the PR record
    # plus push intent on proposals. All nullable.
    if "claim_token" not in br_columns:
        conn.execute("ALTER TABLE bug_reports ADD COLUMN claim_token TEXT")
        conn.commit()
        logger.info("Migration: added bug_reports.claim_token column")
    fp_columns = {row[1] for row in conn.execute("PRAGMA table_info(fix_proposals)").fetchall()}
    for column, sql_type in (
        ("pr_url", "TEXT"), ("pr_number", "INTEGER"),
        ("pushed_sha", "TEXT"), ("pr_opened_at", "TEXT"),
    ):
        if fp_columns and column not in fp_columns:
            conn.execute(f"ALTER TABLE fix_proposals ADD COLUMN {column} {sql_type}")
            conn.commit()
            logger.info("Migration: added fix_proposals.%s column", column)

    # Phase 10 (sonicgrid-ingest): imported-report identity, and the ingest
    # generation that fences in-flight polls against config changes.
    if "ingest_source" not in br_columns:
        conn.execute("ALTER TABLE bug_reports ADD COLUMN ingest_source TEXT")
        conn.commit()
        logger.info("Migration: added bug_reports.ingest_source column")
    if "external_id" not in br_columns:
        conn.execute("ALTER TABLE bug_reports ADD COLUMN external_id TEXT")
        conn.commit()
        logger.info("Migration: added bug_reports.external_id column")
    if "ingest_generation" not in columns:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN ingest_generation INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        logger.info("Migration: added projects.ingest_generation column")
    # Phase 11 (sonicgrid-triage-sync): the triage action that started a
    # stage row, so recovery evidence is exact. Null for worker/API runs.
    an_columns = {row[1] for row in conn.execute("PRAGMA table_info(analyses)").fetchall()}
    if an_columns and "trigger_ref" not in an_columns:
        conn.execute("ALTER TABLE analyses ADD COLUMN trigger_ref TEXT")
        conn.commit()
        logger.info("Migration: added analyses.trigger_ref column")
    fp_columns = {row[1] for row in conn.execute("PRAGMA table_info(fix_proposals)").fetchall()}
    if fp_columns and "trigger_ref" not in fp_columns:
        conn.execute("ALTER TABLE fix_proposals ADD COLUMN trigger_ref TEXT")
        conn.commit()
        logger.info("Migration: added fix_proposals.trigger_ref column")

    br_columns = {row[1] for row in conn.execute("PRAGMA table_info(bug_reports)").fetchall()}
    if {"project_id", "external_id"} <= br_columns:
        # The idempotency key for imports (ingest_commit's ON CONFLICT target).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bug_reports_external "
            "ON bug_reports(project_id, external_id) WHERE external_id IS NOT NULL"
        )
        conn.commit()


_now_lock = threading.Lock()
_last_now_dt: Optional[datetime] = None


def _now() -> str:
    """UTC ISO-8601 timestamp, strictly monotonic within this process.

    Windows' system clock has ~16 ms resolution, so two events created in the
    same tick would otherwise share a `created_at`. Much of this layer sequences
    events by comparing `created_at` lexicographically with strict `>` (and via
    `ORDER BY created_at`); equal timestamps break that ordering — e.g. a fix
    failure recorded in the same tick as its localization would not count toward
    the retry cap. Nudge each call at least one microsecond past the previous so
    sequential inserts always compare strictly increasing.
    """
    global _last_now_dt
    with _now_lock:
        dt = datetime.now(timezone.utc)
        if _last_now_dt is not None and dt <= _last_now_dt:
            dt = _last_now_dt + timedelta(microseconds=1)
        _last_now_dt = dt
        return dt.isoformat()


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
    fix_llm_provider TEXT,
    fix_llm_model TEXT,
    ingest_source TEXT,
    ingest_config TEXT,
    ingest_generation INTEGER NOT NULL DEFAULT 0,
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
    analysis_mode TEXT NOT NULL DEFAULT 'auto',
    claim_token TEXT,
    resolution_reason TEXT,
    assigned_to TEXT,
    ingest_source TEXT,
    external_id TEXT,
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
    trigger_ref TEXT,
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
    pr_url TEXT,
    pr_number INTEGER,
    pushed_sha TEXT,
    pr_opened_at TEXT,
    trigger_ref TEXT,
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
    key_source TEXT,
    key_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_usage_project ON token_usage(project_id);

-- Phase 10 (sonicgrid-ingest): per-project poll checkpoint. `generation` is
-- the projects.ingest_generation this row belongs to (see ingest_commit).
CREATE TABLE IF NOT EXISTS ingest_state (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    cursor TEXT,
    rewalk_cursor TEXT,
    rewalk_started_at TEXT,
    last_full_walk_at TEXT,
    last_poll_at TEXT,
    last_ok_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    imported_total INTEGER NOT NULL DEFAULT 0
);

-- Phase 11 (sonicgrid-triage-sync): one row per pushed report. `fingerprint`,
-- `revision` and `payload` are written together before each PUT, so a retry
-- resends the same revision with the same payload.
CREATE TABLE IF NOT EXISTS triage_results (
    report_id TEXT PRIMARY KEY REFERENCES bug_reports(id),
    project_id TEXT NOT NULL,
    fingerprint TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    payload TEXT,
    acked_fingerprint TEXT,
    failed_fingerprint TEXT,
    last_status INTEGER,
    last_error TEXT,
    pushed_at TEXT
);

-- Phase 11: the action ledger, keyed by sonicgrid action id. `outcome`,
-- `message` and `steps` are written once (conditional on phase != finished).
CREATE TABLE IF NOT EXISTS triage_actions (
    action_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    params TEXT,
    requested_by_user_id TEXT,
    phase TEXT NOT NULL,
    no_auto_retry INTEGER NOT NULL DEFAULT 0,
    pinned_llm TEXT,
    reserved_at TEXT NOT NULL,
    intent_at TEXT,
    dispatched_at TEXT,
    finished_at TEXT,
    outcome TEXT,
    message TEXT,
    steps TEXT,
    fix_proposal_id TEXT,
    pr_url TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    terminal_acked INTEGER NOT NULL DEFAULT 0,
    terminal_error TEXT,
    late_outcome TEXT
);

CREATE INDEX IF NOT EXISTS idx_triage_actions_project ON triage_actions(project_id, phase);

-- Phase 11: per-project tick bookkeeping (fixed error codes only).
CREATE TABLE IF NOT EXISTS triage_sync_state (
    project_id TEXT PRIMARY KEY,
    last_tick_at TEXT,
    last_ok_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    unresolved_actions INTEGER NOT NULL DEFAULT 0
);
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
    fix_llm_provider: Optional[str] = None,
    fix_llm_model: Optional[str] = None,
    ingest_source: Optional[str] = None,
    ingest_config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    conn = _get_conn()
    row_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO projects (id, name, repo_url, default_branch, llm_provider, llm_model,
                                 fix_llm_provider, fix_llm_model, ingest_source, ingest_config,
                                 created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row_id, name, repo_url, default_branch, llm_provider, llm_model,
         fix_llm_provider, fix_llm_model, ingest_source,
         json.dumps(ingest_config) if ingest_config is not None else None,
         now, now),
    )
    conn.commit()
    return project_get(row_id)  # type: ignore[return-value]


def _project_row(row: Any) -> dict[str, Any]:
    """Row -> dict with `ingest_config` deserialized from its JSON column."""
    d = dict(row)
    raw = d.get("ingest_config")
    if isinstance(raw, str):
        try:
            d["ingest_config"] = json.loads(raw)
        except json.JSONDecodeError:
            d["ingest_config"] = None
    return d


def project_get(project_id: str) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _project_row(row) if row else None


def project_list() -> list[dict[str, Any]]:
    conn = _get_conn()
    return [_project_row(r) for r in conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()]


def project_update(project_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    existing = project_get(project_id)
    if not existing:
        return None
    # Phase 10: a change to the ingest source or config invalidates the poll
    # checkpoint (a cursor from one source means nothing to another) and any
    # poll already in flight. Bump the generation and drop the state row in
    # the same transaction as the update; ingest_commit fences on it.
    ingest_changed = False
    if "ingest_source" in fields or "ingest_config" in fields:
        new_source = fields.get("ingest_source", existing.get("ingest_source"))
        new_config = fields.get("ingest_config", existing.get("ingest_config"))
        # Phase 11: `triage_credential_env` configures the triage sync, not
        # the poll, so toggling it must not reset B1's checkpoint.
        ingest_changed = (new_source, _poll_identity(new_config)) != (
            existing.get("ingest_source"), _poll_identity(existing.get("ingest_config"))
        )
    if isinstance(fields.get("ingest_config"), dict):
        fields["ingest_config"] = json.dumps(fields["ingest_config"])
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    if ingest_changed:
        set_clause += ", ingest_generation = ingest_generation + 1"
    values = list(fields.values()) + [project_id]
    conn.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
    if ingest_changed:
        conn.execute("DELETE FROM ingest_state WHERE project_id = ?", (project_id,))
    conn.commit()
    return project_get(project_id)


def _poll_identity(config: Any) -> Any:
    if isinstance(config, dict):
        return {k: v for k, v in config.items() if k != "triage_credential_env"}
    return config


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
    # Phase 11: result rows reference reports, so they go first.
    conn.execute("DELETE FROM triage_results WHERE project_id = ?", (project_id,))
    # Clean up soft-deleted reports before removing the project (FK constraint).
    conn.execute(
        "DELETE FROM bug_reports WHERE project_id = ? AND resolution_reason = 'deleted'",
        (project_id,),
    )
    # Phase 10: the FK cascades too, but deletion must not depend on the pragma.
    conn.execute("DELETE FROM ingest_state WHERE project_id = ?", (project_id,))
    # Phase 11: the rest of the triage ledger.
    for table in ("triage_actions", "triage_sync_state"):
        conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
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
    analysis_mode: str = "auto",
) -> dict[str, Any]:
    conn = _get_conn()
    row_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO bug_reports
           (id, project_id, title, description, reporter,
            steps_to_reproduce, expected_behavior, actual_behavior,
            url, feature_area, severity, environment, labels,
            status, analysis_mode, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?, ?)""",
        (
            row_id, project_id, title, description, reporter,
            _serialize_json(steps_to_reproduce), expected_behavior, actual_behavior,
            url, feature_area, severity, environment, _serialize_json(labels),
            analysis_mode, now, now,
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


def _report_filter_clause(
    project_id: Optional[str],
    status: Optional[str],
    include_deleted: bool,
) -> tuple[str, list[Any]]:
    """Shared WHERE clause for report_list / report_count."""
    clause = " WHERE 1=1"
    params: list[Any] = []
    if not include_deleted:
        clause += " AND (resolution_reason IS NULL OR resolution_reason != 'deleted')"
    if project_id:
        clause += " AND project_id = ?"
        params.append(project_id)
    if status:
        clause += " AND status = ?"
        params.append(status)
    return clause, params


def report_list(
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    include_deleted: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
    order: str = "desc",
) -> list[dict[str, Any]]:
    """List reports, newest first by default.

    `limit`/`offset` paginate at the SQL level (§5.4 dashboard); `order`
    is 'desc' (default) or 'asc' on created_at — anything else falls back
    to 'desc' (never interpolated raw into SQL).
    """
    conn = _get_conn()
    clause, params = _report_filter_clause(project_id, status, include_deleted)
    direction = "ASC" if order == "asc" else "DESC"
    query = f"SELECT * FROM bug_reports{clause} ORDER BY created_at {direction}"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return [_report_row_to_dict(r) for r in conn.execute(query, params).fetchall()]


def report_count(
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    include_deleted: bool = False,
) -> int:
    """Count reports matching the same filters as report_list (pre-pagination)."""
    conn = _get_conn()
    clause, params = _report_filter_clause(project_id, status, include_deleted)
    row = conn.execute(f"SELECT COUNT(*) AS cnt FROM bug_reports{clause}", params).fetchone()
    return int(row["cnt"])


_UNSET: Any = object()


@retry_on_locked
def report_update_status(
    report_id: str,
    new_status: str,
    resolution_reason: Optional[str] = None,
    *,
    expected_status: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Set a report's status.

    With `expected_status` (public PATCH), the write is a compare-and-set on
    the status the caller validated against; it returns None when the row
    changed in between (e.g. open-pr claimed it), and nothing is written.
    """
    conn = _get_conn()
    now = _now()
    fields = {"status": new_status, "updated_at": now}
    if resolution_reason is not None:
        fields["resolution_reason"] = resolution_reason
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [report_id]
    where = "id = ?"
    if expected_status is not None:
        where += " AND status = ?"
        values.append(expected_status)
    cursor = conn.execute(f"UPDATE bug_reports SET {set_clause} WHERE {where}", values)
    conn.commit()
    if expected_status is not None and cursor.rowcount != 1:
        return None
    return report_get(report_id)


def report_delete(
    report_id: str,
    *,
    expected_status: Optional[str] = None,
    expected_claim_token: Any = _UNSET,
) -> bool:
    """Soft-delete a report by setting status to 'rejected' with resolution_reason 'deleted'.

    One conditional UPDATE. With `expected_status` / `expected_claim_token`
    (the public DELETE route), it only applies if the row still has the
    status and claim token the caller checked, so an open-pr claim taken in
    between is never overwritten. Returns False when nothing was deleted.
    """
    conn = _get_conn()
    where = "id = ? AND (resolution_reason IS NULL OR resolution_reason != 'deleted')"
    params: list[Any] = [_now(), report_id]
    if expected_status is not None:
        where += " AND status = ?"
        params.append(expected_status)
    if expected_claim_token is not _UNSET:
        where += " AND claim_token IS ?"
        params.append(expected_claim_token)
    cursor = conn.execute(
        "UPDATE bug_reports SET status = 'rejected', resolution_reason = 'deleted', "
        f"updated_at = ? WHERE {where}",
        params,
    )
    conn.commit()
    return cursor.rowcount == 1


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
def try_claim_report(
    report_id: str,
    expected_status: str,
    new_status: str,
    *,
    claim_token: Optional[str] = None,
) -> bool:
    """Atomically claim a report by transitioning its status.

    With `claim_token` (Phase 8 open-pr), the same compare-and-set also
    records the claim's owner token.

    Returns True only if this caller won the claim (rowcount == 1).
    """
    conn = _get_conn()
    now = _now()
    if claim_token is None:
        cursor = conn.execute(
            "UPDATE bug_reports SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (new_status, now, report_id, expected_status),
        )
    else:
        cursor = conn.execute(
            "UPDATE bug_reports SET status = ?, claim_token = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (new_status, claim_token, now, report_id, expected_status),
        )
    conn.commit()
    return cursor.rowcount == 1


# Transient stage claims and the status each one rolls back to; the same
# rollbacks the pipeline's own failure paths perform. `fix_approved` is not
# listed: open-pr owns its claim and adopts abandoned ones (`adopt_claim`).
ORPHANED_CLAIM_ROLLBACK = {
    "validating": "submitted",
    "analyzing": "triaged",
    "fix_proposing": "triaged",
}


INTERRUPTED_ERROR = "interrupted: the process stopped mid-stage"


@retry_on_locked
def release_orphaned_claims() -> dict[str, int]:
    """Roll back stage claims left behind by a process that died mid-stage.

    Call once at startup, before any worker or sync loop starts. Bugalizer
    runs one process per database (docs/decision_log.md, "Single-process
    assumption"), so nothing in the new process can hold these claims yet:
    every report still sitting in one, and every `running` analysis row,
    belongs to a dead process. A second server on the same database would
    have its live claims rolled back.

    The interrupted attempt is recorded before the report becomes
    schedulable again, so retry caps count it:
    - `running` triage/localization rows become failed rows (transient);
    - each `fix_proposing` report gets a failed `fix` row marked permanent.
      The paid call may already have been billed, so the worker never
      repeats it on its own; a person re-runs it (Analyze, or
      `/queue/{id}/retry`), as with sync's `no_auto_retry`.
    One transaction; a second run finds nothing left to record.
    Returns {claimed_status: count}.
    """
    conn = _get_conn()
    now = _now()
    released: dict[str, int] = {}
    with conn:
        conn.execute(
            "UPDATE analyses SET status = 'failed', result = ?, completed_at = ? "
            "WHERE status = 'running'",
            (json.dumps({"error": INTERRUPTED_ERROR, "interrupted": True}), now),
        )
        fixing = conn.execute(
            "SELECT id FROM bug_reports WHERE status = 'fix_proposing'"
        ).fetchall()
        fix_result = json.dumps(
            {"error": INTERRUPTED_ERROR, "interrupted": True, "permanent": True}
        )
        for row in fixing:
            conn.execute(
                """INSERT INTO analyses
                   (id, bug_report_id, phase, status, result, completed_at, created_at)
                   VALUES (?, ?, 'fix', 'failed', ?, ?, ?)""",
                (_new_id(), row["id"], fix_result, now, now),
            )
        for claimed, rollback in ORPHANED_CLAIM_ROLLBACK.items():
            cursor = conn.execute(
                "UPDATE bug_reports SET status = ?, updated_at = ? WHERE status = ?",
                (rollback, now, claimed),
            )
            if cursor.rowcount:
                released[claimed] = cursor.rowcount
    return released


@retry_on_locked
def adopt_claim(report_id: str, observed_token: Optional[str], new_token: str) -> bool:
    """Take over an abandoned `fix_approved` claim (Phase 8).

    Compare-and-set on the token the caller observed, so of two requests
    racing to adopt the same claim only one wins.
    """
    conn = _get_conn()
    cursor = conn.execute(
        "UPDATE bug_reports SET claim_token = ?, updated_at = ? "
        "WHERE id = ? AND status = 'fix_approved' AND claim_token IS ?",
        (new_token, _now(), report_id, observed_token),
    )
    conn.commit()
    return cursor.rowcount == 1


@retry_on_locked
def release_claim(report_id: str, claim_token: str) -> bool:
    """Return a claimed report to `fix_proposed` and clear its token.

    Only the claim's owner can release it (CAS on the token).
    """
    conn = _get_conn()
    cursor = conn.execute(
        "UPDATE bug_reports SET status = 'fix_proposed', claim_token = NULL, updated_at = ? "
        "WHERE id = ? AND status = 'fix_approved' AND claim_token = ?",
        (_now(), report_id, claim_token),
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
    trigger_ref: Optional[str] = None,
) -> dict[str, Any]:
    conn = _get_conn()
    row_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO analyses
           (id, bug_report_id, phase, status, result,
            llm_provider, llm_model, prompt_tokens, completion_tokens,
            estimated_cost_usd, started_at, completed_at, trigger_ref, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row_id, bug_report_id, phase, status,
            json.dumps(result) if result else None,
            llm_provider, llm_model, prompt_tokens, completion_tokens,
            estimated_cost_usd, started_at, completed_at, trigger_ref, now,
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
    - analysis_mode is not 'hold' (§5.3: hold reports never auto-dispatch
      to any LLM stage; validation/dedupe already ran in Stage 1)
    """
    conn = _get_conn()
    max_retries = settings.max_triage_retries
    retry_delay = settings.retry_delay_seconds

    # Get all triaged, non-deleted reports
    reports = [
        _report_row_to_dict(r) for r in conn.execute(
            """SELECT * FROM bug_reports
               WHERE status = 'triaged'
               AND COALESCE(analysis_mode, 'auto') != 'hold'
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
    - analysis_mode is not 'hold' (§5.3). `local_only` reports ARE eligible —
      localization is a local stage; the mode only stops Stage 4.
    """
    conn = _get_conn()
    now = datetime.now(timezone.utc)

    # Get triaged reports with completed triage and project with repo_path
    rows = conn.execute(
        """SELECT br.*, p.repo_path, p.default_branch, p.head_sha AS project_head_sha
           FROM bug_reports br
           JOIN projects p ON br.project_id = p.id
           WHERE br.status = 'triaged'
           AND COALESCE(br.analysis_mode, 'auto') != 'hold'
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
    trigger_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a new fix_proposals row and return the created record."""
    conn = _get_conn()
    row_id = _new_id()
    now = _now()
    conn.execute(
        """INSERT INTO fix_proposals
           (id, bug_report_id, analysis_id, branch_name, diff, explanation,
            confidence, root_cause, files_changed, status, trigger_ref,
            created_at, updated_at)
           VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?)""",
        (row_id, bug_report_id, analysis_id, diff, explanation,
         confidence, root_cause, json.dumps(files_changed), trigger_ref, now, now),
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


def fix_proposal_of_report(bug_report_id: str, proposal_id: str) -> Optional[dict[str, Any]]:
    """The proposal with this ID if it belongs to this report, else None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM fix_proposals WHERE id = ? AND bug_report_id = ?",
        (proposal_id, bug_report_id),
    ).fetchone()
    return _fix_proposal_row_to_dict(row) if row else None


def recorded_pull_request(bug_report_id: str) -> Optional[dict[str, Any]]:
    """The report's proposal that has a recorded PR, if any (one PR per report)."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT * FROM fix_proposals
           WHERE bug_report_id = ? AND pr_url IS NOT NULL
           ORDER BY pr_opened_at ASC LIMIT 1""",
        (bug_report_id,),
    ).fetchone()
    return _fix_proposal_row_to_dict(row) if row else None


@retry_on_locked
def fix_proposal_record_push_intent(proposal_id: str, pushed_sha: str, branch_name: str) -> None:
    """Record the commit about to be pushed, before the push (Phase 8).

    A retry that finds the branch on the remote with this tip knows the push
    was ours and resumes at the PR POST instead of refusing the branch.
    """
    conn = _get_conn()
    conn.execute(
        "UPDATE fix_proposals SET pushed_sha = ?, branch_name = ?, updated_at = ? WHERE id = ?",
        (pushed_sha, branch_name, _now(), proposal_id),
    )
    conn.commit()


class ClaimLostError(RuntimeError):
    """The open-pr claim no longer belongs to this request."""


def _record_pr_proposal(
    conn: sqlite3.Connection,
    bug_report_id: str,
    proposal_id: str,
    *,
    pr_url: str,
    pr_number: int,
    pushed_sha: Optional[str],
    branch_name: str,
    now: str,
) -> None:
    cursor = conn.execute(
        """UPDATE fix_proposals
           SET pr_url = ?, pr_number = ?, pushed_sha = COALESCE(?, pushed_sha),
               branch_name = ?, pr_opened_at = ?, status = 'pr_opened', updated_at = ?
           WHERE id = ? AND bug_report_id = ?""",
        (pr_url, pr_number, pushed_sha, branch_name, now, now, proposal_id, bug_report_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("proposal does not belong to the report")


def _record_pr_report(
    conn: sqlite3.Connection, bug_report_id: str, claim_token: str, now: str
) -> None:
    cursor = conn.execute(
        """UPDATE bug_reports SET status = 'fix_committed', claim_token = NULL, updated_at = ?
           WHERE id = ? AND status = 'fix_approved' AND claim_token = ?""",
        (now, bug_report_id, claim_token),
    )
    if cursor.rowcount != 1:
        raise ClaimLostError("open-pr claim lost before the PR was recorded")


@retry_on_locked
def record_pull_request(
    bug_report_id: str,
    proposal_id: str,
    claim_token: str,
    *,
    pr_url: str,
    pr_number: int,
    pushed_sha: Optional[str],
    branch_name: str,
) -> None:
    """Record an opened PR against its owning proposal and move the report
    `fix_approved -> fix_committed`, in one transaction (Phase 8).

    `proposal_id` must belong to the report. The report update is a CAS on
    the caller's claim token; if it matches no row, the claim was lost and
    nothing is written. `pushed_sha=None` keeps the proposal's recorded value.
    """
    conn = _get_conn()
    now = _now()
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _record_pr_proposal(
            conn, bug_report_id, proposal_id,
            pr_url=pr_url, pr_number=pr_number, pushed_sha=pushed_sha,
            branch_name=branch_name, now=now,
        )
        _record_pr_report(conn, bug_report_id, claim_token, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def fix_analysis_for_proposal(proposal: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The completed Stage 4 analysis that produced a proposal: the newest
    completed `fix` row created no later than the proposal (the stage writes
    the analysis immediately before the proposal)."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT * FROM analyses
           WHERE bug_report_id = ? AND phase = 'fix' AND status = 'completed'
           AND created_at <= ?
           ORDER BY created_at DESC LIMIT 1""",
        (proposal["bug_report_id"], proposal["created_at"]),
    ).fetchone()
    return _analysis_row_to_dict(row) if row else None


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
    - analysis_mode is 'auto' (§5.3): `local_only` and `hold` reports are
      never auto-dispatched to the paid cloud stage. An explicit
      `POST /reports/{id}/analyze {"tier": "cloud"}` bypasses this gate —
      the mode governs *automatic* dispatch, not manual requests.
    - Not soft-deleted.
    """
    conn = _get_conn()
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT br.*, p.head_sha AS project_head_sha
           FROM bug_reports br
           JOIN projects p ON br.project_id = p.id
           WHERE br.status = 'triaged'
           AND COALESCE(br.analysis_mode, 'auto') = 'auto'
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


def report_ids_with_localization() -> set[str]:
    """Set of report IDs with at least one completed localization analysis.

    One query for the whole board — lets the reports list flag analyzed cards
    without an N+1 per-report lookup.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT bug_report_id FROM analyses "
        "WHERE phase = 'localization' AND status = 'completed'"
    ).fetchall()
    return {row["bug_report_id"] for row in rows}


def report_tier_summary(
    bug_report_id: Optional[str] = None,
) -> dict[str, dict[str, Optional[str]]]:
    """Latest completed LLM-analysis timestamp per tier, keyed by report ID.

    Tier is derived from the analysis row's ``llm_provider``: ``ollama`` is
    the local (free) tier; any other non-null provider is a paid cloud tier
    (§5b.1 scan-state chips). One aggregate query for the whole board — the
    reports list must not do an N+1 per-card lookup.
    """
    conn = _get_conn()
    query = (
        "SELECT bug_report_id, "
        "  MAX(CASE WHEN llm_provider = 'ollama' THEN completed_at END) AS local_at, "
        "  MAX(CASE WHEN llm_provider IS NOT NULL AND llm_provider != 'ollama' "
        "      THEN completed_at END) AS cloud_at "
        "FROM analyses WHERE status = 'completed' AND completed_at IS NOT NULL"
    )
    params: tuple[str, ...] = ()
    if bug_report_id is not None:
        query += " AND bug_report_id = ?"
        params = (bug_report_id,)
    query += " GROUP BY bug_report_id"
    return {
        row["bug_report_id"]: {"local": row["local_at"], "cloud": row["cloud_at"]}
        for row in conn.execute(query, params).fetchall()
    }


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
    key_source: Optional[str] = None,
    key_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Record one LLM call's usage.

    `key_source` (`request` | `env`) and `key_ref` (the caller's opaque
    settings-row reference) are the Phase 7 attribution columns. The key
    itself is never passed here.
    """
    conn = _get_conn()
    now = _now()
    conn.execute(
        """INSERT INTO token_usage
           (project_id, bug_report_id, provider, model,
            prompt_tokens, completion_tokens, estimated_cost_usd,
            key_source, key_ref, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, bug_report_id, provider, model,
         prompt_tokens, completion_tokens, estimated_cost_usd,
         key_source, key_ref, now),
    )
    conn.commit()
    return {
        "project_id": project_id,
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "key_source": key_source,
        "key_ref": key_ref,
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

    # Phase 7: the same totals grouped by who paid (key_source + key_ref).
    # Additive to the aggregate above; pre-Phase-7 rows group under nulls.
    attr_query = """SELECT key_source, key_ref,
                    COUNT(*) as calls,
                    SUM(prompt_tokens) as total_prompt,
                    SUM(completion_tokens) as total_completion,
                    SUM(estimated_cost_usd) as total_cost
                    FROM token_usage"""
    if project_id:
        attr_query += " WHERE project_id = ?"
    attr_query += " GROUP BY key_source, key_ref ORDER BY key_source, key_ref"
    attribution = [
        {
            "key_source": r["key_source"],
            "key_ref": r["key_ref"],
            "calls": r["calls"],
            "prompt_tokens": r["total_prompt"],
            "completion_tokens": r["total_completion"],
            "estimated_cost_usd": r["total_cost"],
        }
        for r in conn.execute(attr_query, params).fetchall()
    ]

    return {
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_estimated_cost_usd": total_cost,
        "by_provider": by_provider,
        "attribution": attribution,
    }


# ---------------------------------------------------------------------------
# Ingest (Phase 10 / B1 sonicgrid-ingest)
# ---------------------------------------------------------------------------

# Columns of ingest_state a poll may set directly (imported_total and
# consecutive_failures move only through ingest_commit's counters).
_INGEST_STATE_FIELDS = frozenset({
    "cursor", "rewalk_cursor", "rewalk_started_at", "last_full_walk_at",
    "last_poll_at", "last_ok_at", "last_error", "consecutive_failures",
})


def projects_with_ingest() -> list[dict[str, Any]]:
    """Projects that have an ingest source configured, oldest first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM projects WHERE ingest_source IS NOT NULL ORDER BY created_at"
    ).fetchall()
    return [_project_row(r) for r in rows]


def ingest_state_get(project_id: str) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM ingest_state WHERE project_id = ?", (project_id,)
    ).fetchone()
    return dict(row) if row else None


def ingest_health_counts() -> dict[str, int]:
    """Aggregate, non-identifying ingest counts for the public /health."""
    conn = _get_conn()
    projects = conn.execute(
        "SELECT COUNT(*) FROM projects WHERE ingest_source IS NOT NULL"
    ).fetchone()[0]
    failing = conn.execute(
        """SELECT COUNT(*) FROM ingest_state s JOIN projects p ON p.id = s.project_id
           WHERE p.ingest_source IS NOT NULL AND s.last_error IS NOT NULL"""
    ).fetchone()[0]
    return {"projects": projects, "failing": failing}


@retry_on_locked
def ingest_commit(
    project_id: str,
    generation: int,
    *,
    reports: Sequence[dict[str, Any]] = (),
    updates: Optional[dict[str, Any]] = None,
    count_failure: bool = False,
) -> Optional[int]:
    """Write one poll step — imported reports plus checkpoint/bookkeeping
    updates — in a single transaction fenced by the ingest generation.

    The poll captured `generation` (projects.ingest_generation) after taking
    its lock. A config change, clear or project delete since then bumps or
    removes it; the fence then rolls back with nothing written and returns
    None, so a slow in-flight poll can never import old-source rows or
    recreate a reset checkpoint. Otherwise returns the number of reports
    inserted (existing (project_id, external_id) pairs are skipped).

    Because imports and checkpoint commit together, a failure mid-page leaves
    both untouched; the page replays and is never skipped.
    """
    updates = dict(updates or {})
    unknown = set(updates) - _INGEST_STATE_FIELDS
    if unknown:
        raise ValueError(f"unknown ingest_state fields: {sorted(unknown)}")
    conn = _get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT ingest_generation, ingest_source FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if row is None or row["ingest_source"] is None or row["ingest_generation"] != generation:
            conn.rollback()
            return None
        now = _now()
        conn.execute(
            "DELETE FROM ingest_state WHERE project_id = ? AND generation != ?",
            (project_id, generation),
        )
        # A new checkpoint counts its creation as the last full walk: the
        # first forward walk starts from the beginning anyway, so the first
        # reconciliation re-walk is due one interval later, not immediately.
        conn.execute(
            """INSERT INTO ingest_state (project_id, generation, last_full_walk_at)
               VALUES (?, ?, ?) ON CONFLICT(project_id) DO NOTHING""",
            (project_id, generation, now),
        )
        inserted = 0
        for r in reports:
            cur = conn.execute(
                """INSERT INTO bug_reports
                   (id, project_id, title, description, reporter, severity,
                    environment, attachments, labels, status, analysis_mode,
                    ingest_source, external_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', 'auto', ?, ?, ?, ?)
                   ON CONFLICT(project_id, external_id) WHERE external_id IS NOT NULL
                   DO NOTHING""",
                (
                    _new_id(), project_id, r["title"], r["description"], r["reporter"],
                    r.get("severity", "medium"), r.get("environment"),
                    _serialize_json(r.get("attachments")), _serialize_json(r.get("labels")),
                    r["ingest_source"], r["external_id"], _now(), _now(),
                ),
            )
            inserted += cur.rowcount
        sets = [f"{k} = ?" for k in updates]
        values: list[Any] = list(updates.values())
        sets.append("imported_total = imported_total + ?")
        values.append(inserted)
        if count_failure:
            sets.append("consecutive_failures = consecutive_failures + 1")
        conn.execute(
            f"UPDATE ingest_state SET {', '.join(sets)} WHERE project_id = ?",
            values + [project_id],
        )
        conn.commit()
        return inserted
    except BaseException:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Triage sync (Phase 11 / B3 sonicgrid-triage-sync)
# ---------------------------------------------------------------------------

def projects_with_triage_sync() -> list[dict[str, Any]]:
    """Projects whose ingest config names a triage credential, oldest first."""
    return [
        p for p in projects_with_ingest()
        if isinstance(p.get("ingest_config"), dict)
        and p["ingest_config"].get("triage_credential_env")
    ]


def sync_reports(project_id: str) -> list[dict[str, Any]]:
    """Sonicgrid-sourced reports of a project (those with an external id)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM bug_reports WHERE project_id = ? AND external_id IS NOT NULL "
        "ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [_report_row_to_dict(r) for r in rows]


def report_by_external_id(project_id: str, external_id: str) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM bug_reports WHERE project_id = ? AND external_id = ?",
        (project_id, external_id),
    ).fetchone()
    return _report_row_to_dict(row) if row else None


def triage_result_get(report_id: str) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM triage_results WHERE report_id = ?", (report_id,)
    ).fetchone()
    return dict(row) if row else None


@retry_on_locked
def triage_result_stage(
    report_id: str, project_id: str, fingerprint: str, revision: int, payload: str
) -> None:
    """Record the fingerprint, revision and payload of the next push, together,
    before the PUT (one revision always means one payload)."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO triage_results (report_id, project_id, fingerprint, revision, payload)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(report_id) DO UPDATE SET
             fingerprint = excluded.fingerprint, revision = excluded.revision,
             payload = excluded.payload, failed_fingerprint = NULL,
             last_error = NULL""",
        (report_id, project_id, fingerprint, revision, payload),
    )
    conn.commit()


_TRIAGE_RESULT_FIELDS = frozenset({
    "revision", "acked_fingerprint", "failed_fingerprint",
    "last_status", "last_error", "pushed_at",
})


@retry_on_locked
def triage_result_update(report_id: str, **fields: Any) -> None:
    unknown = set(fields) - _TRIAGE_RESULT_FIELDS
    if unknown:
        raise ValueError(f"unknown triage_results fields: {sorted(unknown)}")
    conn = _get_conn()
    conn.execute(
        f"UPDATE triage_results SET {', '.join(f'{k} = ?' for k in fields)} WHERE report_id = ?",
        [*fields.values(), report_id],
    )
    conn.commit()


def _triage_action_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    for key in ("params", "pinned_llm", "steps"):
        if isinstance(d.get(key), str):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                d[key] = None
    return d


def triage_action_get(action_id: str) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM triage_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    return _triage_action_row(row) if row else None


@retry_on_locked
def triage_action_reserve(
    action_id: str,
    project_id: str,
    report_id: str,
    kind: str,
    params: dict[str, Any],
    requested_by_user_id: Optional[str],
) -> bool:
    """Create the ledger row (`phase='reserved'`). True only for the caller
    that inserted it; an existing row means the action is not new."""
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO triage_actions
           (action_id, project_id, report_id, kind, params, requested_by_user_id,
            phase, reserved_at)
           VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)
           ON CONFLICT(action_id) DO NOTHING""",
        (action_id, project_id, report_id, kind, json.dumps(params),
         requested_by_user_id, _now()),
    )
    conn.commit()
    return cur.rowcount == 1


@retry_on_locked
def triage_action_delete_reserved(action_id: str) -> None:
    """Drop a reservation whose claim sonicgrid refused (nothing ran)."""
    conn = _get_conn()
    conn.execute(
        "DELETE FROM triage_actions WHERE action_id = ? AND phase = 'reserved'", (action_id,)
    )
    conn.commit()


_TRIAGE_ACTION_FIELDS = frozenset({
    "phase", "no_auto_retry", "pinned_llm", "intent_at", "dispatched_at",
    "fix_proposal_id", "pr_url", "attempts", "steps", "terminal_acked",
    "terminal_error", "late_outcome",
})


@retry_on_locked
def triage_action_update(
    action_id: str, *, expected_phase: Optional[str] = None, **fields: Any
) -> bool:
    """Update ledger fields; with `expected_phase`, only if the row is still in
    it. Never touches a finished row's outcome (see triage_action_finish)."""
    unknown = set(fields) - _TRIAGE_ACTION_FIELDS
    if unknown:
        raise ValueError(f"unknown triage_actions fields: {sorted(unknown)}")
    for key in ("pinned_llm", "steps"):
        if key in fields and not isinstance(fields[key], (str, type(None))):
            fields[key] = json.dumps(fields[key])
    conn = _get_conn()
    where = "action_id = ?"
    params: list[Any] = [*fields.values(), action_id]
    if expected_phase is not None:
        where += " AND phase = ?"
        params.append(expected_phase)
    cur = conn.execute(
        f"UPDATE triage_actions SET {', '.join(f'{k} = ?' for k in fields)} WHERE {where}",
        params,
    )
    conn.commit()
    return cur.rowcount == 1


@retry_on_locked
def triage_action_finish(
    action_id: str,
    outcome: str,
    message: Optional[str],
    steps: Optional[list[dict[str, Any]]] = None,
    *,
    expected_phase: Optional[str] = None,
) -> bool:
    """Write the terminal outcome once. False (and nothing written) when the
    row is already finished, or not in `expected_phase`."""
    conn = _get_conn()
    where = "action_id = ? AND phase != 'finished'"
    params: list[Any] = [outcome, message, json.dumps(steps) if steps is not None else None,
                         _now(), action_id]
    if expected_phase is not None:
        where += " AND phase = ?"
        params.append(expected_phase)
    cur = conn.execute(
        f"""UPDATE triage_actions
            SET phase = 'finished', outcome = ?, message = ?, steps = ?, finished_at = ?
            WHERE {where}""",
        params,
    )
    conn.commit()
    return cur.rowcount == 1


def triage_actions_unacked(project_id: str) -> list[dict[str, Any]]:
    """Finished actions whose terminal POST is not yet acknowledged."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM triage_actions WHERE project_id = ? AND phase = 'finished' "
        "AND terminal_acked = 0 ORDER BY finished_at",
        (project_id,),
    ).fetchall()
    return [_triage_action_row(r) for r in rows]


def triage_actions_open(project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Authorized, unfinished actions (intent, dispatched, fix_done)."""
    conn = _get_conn()
    query = ("SELECT * FROM triage_actions WHERE phase IN ('intent', 'dispatched', 'fix_done')")
    params: list[Any] = []
    if project_id is not None:
        query += " AND project_id = ?"
        params.append(project_id)
    return [_triage_action_row(r) for r in conn.execute(query, params).fetchall()]


def triage_action_counts(project_id: str) -> dict[str, int]:
    conn = _get_conn()
    counts = {
        r["phase"]: r["n"]
        for r in conn.execute(
            "SELECT phase, COUNT(*) AS n FROM triage_actions WHERE project_id = ? "
            "AND NOT (phase = 'finished' AND terminal_acked = 1) GROUP BY phase",
            (project_id,),
        ).fetchall()
    }
    counts["acked"] = conn.execute(
        "SELECT COUNT(*) FROM triage_actions WHERE project_id = ? AND phase = 'finished' "
        "AND terminal_acked = 1",
        (project_id,),
    ).fetchone()[0]
    return counts


def triage_terminal_error_count(project_id: str) -> int:
    conn = _get_conn()
    return conn.execute(
        "SELECT COUNT(*) FROM triage_actions WHERE project_id = ? AND terminal_error IS NOT NULL",
        (project_id,),
    ).fetchone()[0]


def analyses_by_trigger(trigger_ref: str) -> list[dict[str, Any]]:
    """Stage rows a triage action started, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM analyses WHERE trigger_ref = ? ORDER BY created_at DESC",
        (trigger_ref,),
    ).fetchall()
    return [_analysis_row_to_dict(r) for r in rows]


def fix_proposals_by_trigger(trigger_ref: str) -> list[dict[str, Any]]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM fix_proposals WHERE trigger_ref = ? ORDER BY created_at DESC",
        (trigger_ref,),
    ).fetchall()
    return [_fix_proposal_row_to_dict(r) for r in rows]


def triage_results_summary(project_id: str) -> dict[str, Any]:
    conn = _get_conn()
    tracked = conn.execute(
        "SELECT COUNT(*) FROM triage_results WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM triage_results WHERE project_id = ? "
        "AND fingerprint IS NOT acked_fingerprint AND fingerprint IS NOT failed_fingerprint",
        (project_id,),
    ).fetchone()[0]
    errors = [
        r["report_id"] for r in conn.execute(
            "SELECT report_id FROM triage_results WHERE project_id = ? "
            "AND last_error IS NOT NULL ORDER BY report_id",
            (project_id,),
        ).fetchall()
    ]
    return {"tracked": tracked, "pending": pending, "errors": errors}


def triage_sync_state_get(project_id: str) -> Optional[dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM triage_sync_state WHERE project_id = ?", (project_id,)
    ).fetchone()
    return dict(row) if row else None


@retry_on_locked
def triage_sync_state_record(
    project_id: str, *, error: Optional[str], unresolved_actions: int
) -> int:
    """Record one tick's outcome; returns consecutive failures after it."""
    conn = _get_conn()
    now = _now()
    conn.execute(
        """INSERT INTO triage_sync_state (project_id) VALUES (?)
           ON CONFLICT(project_id) DO NOTHING""",
        (project_id,),
    )
    if error is None:
        conn.execute(
            """UPDATE triage_sync_state SET last_tick_at = ?, last_ok_at = ?, last_error = NULL,
               consecutive_failures = 0, unresolved_actions = ? WHERE project_id = ?""",
            (now, now, unresolved_actions, project_id),
        )
    else:
        conn.execute(
            """UPDATE triage_sync_state SET last_tick_at = ?, last_error = ?,
               consecutive_failures = consecutive_failures + 1, unresolved_actions = ?
               WHERE project_id = ?""",
            (now, error, unresolved_actions, project_id),
        )
    conn.commit()
    return int(conn.execute(
        "SELECT consecutive_failures FROM triage_sync_state WHERE project_id = ?", (project_id,)
    ).fetchone()[0])


def triage_sync_health_counts() -> dict[str, int]:
    """Aggregate, non-identifying triage-sync counts for the public /health."""
    projects = projects_with_triage_sync()
    ids = [p["id"] for p in projects]
    failing = 0
    if ids:
        conn = _get_conn()
        placeholders = ",".join("?" for _ in ids)
        failing = conn.execute(
            f"SELECT COUNT(*) FROM triage_sync_state WHERE project_id IN ({placeholders}) "
            "AND last_error IS NOT NULL",
            ids,
        ).fetchone()[0]
    return {"projects": len(ids), "failing": failing}
