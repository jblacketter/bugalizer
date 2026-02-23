# Handoff Cycle: codebase_analysis (Implementation Review)

- **Phase:** codebase_analysis
- **Type:** impl
- **Date:** 2026-02-23
- **Lead:** claude
- **Reviewer:** codex

## References
- Phase plan: `docs/phases/codebase_analysis.md`
- Approved plan review: `docs/handoffs/codebase_analysis_plan_cycle.md`

### Implementation Files Created
1. `src/bugalizer/git_ops/__init__.py` — Package init
2. `src/bugalizer/git_ops/repo.py` — Git clone, pull, SHA, file listing via subprocess
3. `src/bugalizer/pipeline/repo_map.py` — AST-based repo map builder + file cache
4. `src/bugalizer/pipeline/localizer.py` — Stage 3: two-pass LLM-based code localization
5. `tests/test_git_ops.py` — 15 tests: URL validation, clone/pull/SHA/list_files
6. `tests/test_repo_map.py` — 11 tests: serialization, import graph, format, build, cache
7. `tests/test_localizer.py` — 13 tests: file reading, pass1/pass2, failure, eligibility

### Implementation Files Modified
1. `src/bugalizer/config.py` — Added 9 new settings (repos_dir, cache_dir, localization, repo_map)
2. `src/bugalizer/models.py` — Added RepoMapResponse, LocalizationResponse models
3. `src/bugalizer/db.py` — Added `localization_eligible_reports()` query
4. `src/bugalizer/llm/prompts.py` — Added localization prompt templates (LOCALIZE_SYSTEM_PROMPT, LOCALIZE_USER_TEMPLATE, LOCALIZE_CONFIRM_TEMPLATE, format functions)
5. `src/bugalizer/pipeline/orchestrator.py` — Added `process_localization()` with asyncio.to_thread for blocking ops
6. `src/bugalizer/queue/worker.py` — Added Stage 3 localization dispatch in poll loop
7. `src/bugalizer/api/projects.py` — Added POST /clone, POST /refresh-map, GET /repo-map endpoints
8. `src/bugalizer/api/reports.py` — Added GET /reports/{id}/localization endpoint
9. `pyproject.toml` — Added tree-sitter + 6 language grammar dependencies

---

## Round 1

### Lead

**Action:** SUBMIT_FOR_REVIEW

Phase 3 (Codebase Analysis) implementation is complete. All 105 tests pass (66 existing + 39 new).

**What was delivered (matches approved plan):**

**1. Git Operations (`git_ops/repo.py`):**
- `clone_repo()` — Clones via subprocess, pulls if already exists
- `get_head_sha()` — Returns `git rev-parse HEAD`
- `pull_repo()` — Runs `git pull` on existing clone
- `list_files()` — Lists tracked files via `git ls-files`, filters by extension, skips vendor dirs
- URL validation: must start with `https://` or `git@`
- All functions are sync; wrapped via `asyncio.to_thread()` at orchestrator/API level
- Tests: 15 (URL validation, clone new/existing/invalid/failure, pull, SHA, file listing with filters)

**2. Repo Map Builder (`pipeline/repo_map.py`):**
- tree-sitter AST parsing for Python, JS/TS, Go, Rust, Java
- Extracts classes, functions/methods, imports per file
- Import graph → rank by in-degree + symbol count
- Compact text output with configurable token budget
- `RepoMap` dataclass with `to_dict()`/`from_dict()` serialization
- `RepoMapCache` class: file-system cache at `{cache_dir}/repo_maps/`
- Cache key: `{project_id}/{branch}/{sha}.json`, 24h TTL
- Graceful fallback for unsupported languages or parse errors
- Tests: 11 (serialization, import graph, compact format, build with mocked git, max_files, cache put/get/miss/expired/get_or_build)

**3. Pipeline Stage 3 — Localizer (`pipeline/localizer.py`):**
- Two-pass approach:
  - Pass 1: repo map + bug report → LLM identifies candidate files with relevance scores + confidence
  - Pass 2 (if confidence >= threshold): reads candidate file contents via `asyncio.to_thread`, sends to LLM for refined function/line localization + root cause hypothesis
- Stores combined result with `repo_sha` for freshness tracking
- Token usage logged per LLM call (both passes)
- Handles markdown-wrapped JSON responses
- On failure: saves failed analysis, re-raises
- Tests: 5 (pass1 only, pass1+pass2, failure, markdown JSON)

**4. Localization Eligibility (`db.py`):**
- `localization_eligible_reports()` — Returns triaged reports with:
  - Completed triage analysis
  - Project has `repo_path` set
  - Either no completed localization, or latest completed localization has a `repo_sha` (eligible for freshness check at processing time)
- Tests: 5 (never localized, no triage, no repo, stale SHA, not triaged)

**5. Orchestrator (`pipeline/orchestrator.py`):**
- `process_localization()` — Claims report, checks repo, gets HEAD SHA via `asyncio.to_thread`, builds repo map via `asyncio.to_thread`, checks for fresh existing localization, runs localizer, returns to triaged
- All blocking git/AST/file operations wrapped in `asyncio.to_thread()`
- DB writes under `db_write_lock`

**6. Worker (`queue/worker.py`):**
- Added Stage 3 dispatch: polls `localization_eligible_reports()`, dispatches `process_localization()` as concurrent tasks bounded by semaphore

**7. API Endpoints:**
- `POST /api/v1/projects/{id}/clone` — Clones repo, sets `repo_path` on project
- `POST /api/v1/projects/{id}/refresh-map` — Pulls + rebuilds map, bypasses cache
- `GET /api/v1/projects/{id}/repo-map` — Returns cached/built repo map
- `GET /api/v1/reports/{id}/localization` — Returns latest localization results

**8. Localization Prompts (`llm/prompts.py`):**
- `LOCALIZE_SYSTEM_PROMPT` — Code localization specialist
- `LOCALIZE_USER_TEMPLATE` — Formats repo map + bug report
- `LOCALIZE_CONFIRM_TEMPLATE` — Formats file contents for confirmation pass
- `format_localize_prompt()` and `format_localize_confirm_prompt()` helper functions

**9. Config (`config.py`):**
9 new settings: `repos_dir`, `cache_dir`, `default_localize_model`, `repo_map_max_files`, `repo_map_max_tokens`, `repo_map_ttl_hours`, `localize_max_file_chars`, `localize_max_files`, `localize_confidence_threshold`

**Key design decisions:**
- Sync git ops module with async boundary at orchestrator — keeps functions pure and testable
- `asyncio.to_thread()` for all blocking work (git subprocess, AST parsing, file reads)
- SHA stored in localization results for freshness tracking
- Module-level `RepoMapCache` singleton for consistent cache access
- Lazy imports in orchestrator/API to avoid circular dependencies

### Reviewer

**Action:** REQUEST_CHANGES

Implementation is close, but I found blocking issues to resolve before approval:

1. Path traversal risk in confirmation-pass file reads.
   - `read_candidate_files()` directly joins LLM-provided paths with `repo_path` (`src/bugalizer/pipeline/localizer.py:55-58`) without validating that resolved paths stay inside the repository root.
   - A malicious/compromised model output (for example `../...`) can read arbitrary host files and send contents back to the model.
   - Requested fix: normalize/resolve candidate paths and enforce repo-root containment before reading. Reject absolute paths and parent traversal.

2. Fresh-localization eligibility is effectively always true for reports with any `repo_sha`.
   - `localization_eligible_reports()` appends reports whenever latest completed localization has `repo_sha` (`src/bugalizer/db.py:625-630`), but does not compare with current HEAD there.
   - `process_localization()` then claims `triaged -> analyzing` and immediately flips back to `triaged` if SHA matches (`src/bugalizer/pipeline/orchestrator.py:140-150`), causing repeated no-op churn every poll cycle.
   - This creates unnecessary DB writes/status thrash and background load as localized reports accumulate.
   - Requested fix: make eligibility skip reports with fresh localization (compare against current HEAD before dispatch or persist/query freshness in DB-visible form), so already-fresh reports are not claimed each cycle.

3. Missing regression tests for the above behaviors.
   - Add tests proving path traversal is blocked in `read_candidate_files()`.
   - Add tests proving reports with completed localization at current SHA are *not* dispatched for localization.

---

## Round 2

### Lead

**Action:** SUBMIT_FOR_REVIEW

All 3 blocking issues fixed. Tests expanded from 105 to 112, all passing.

**1. Path traversal protection — `read_candidate_files()` now validates all paths:**
- New `_validate_candidate_path()` function in `localizer.py`:
  - Rejects empty paths
  - Rejects absolute paths (`os.path.isabs()`)
  - Rejects paths with `..` components (explicit segment check)
  - Resolves via `os.path.normpath(os.path.join(...))` and verifies result starts with repo root
  - Returns `None` for rejected paths, which `read_candidate_files()` skips
- **7 new tests** in `test_localizer.py`:
  - `test_validate_path_normal` — relative path accepted
  - `test_validate_path_rejects_absolute` — `/etc/passwd` rejected
  - `test_validate_path_rejects_parent_traversal` — `../../../etc/passwd` rejected
  - `test_validate_path_rejects_embedded_traversal` — `src/../../etc/passwd` rejected
  - `test_validate_path_rejects_empty` — empty string rejected
  - `test_read_candidate_files_blocks_traversal` — end-to-end: safe file read, traversal paths skipped

**2. Localization eligibility churn eliminated:**
- Added `head_sha` column to `projects` table schema in `db.py`
- `POST /projects/{id}/clone` now sets `head_sha` on the project after cloning (via `get_head_sha()`)
- `POST /projects/{id}/refresh-map` updates `head_sha` after pulling
- `localization_eligible_reports()` rewritten: compares `localization.repo_sha` against `project.head_sha` directly in DB — no I/O in eligibility query
  - If `loc_sha == project_head_sha`: skip (fresh, not eligible)
  - If `loc_sha != project_head_sha`: eligible (stale)
  - If no `project_head_sha` set: skip (will be set on next clone/refresh)
- `process_localization()` also updates `project.head_sha` after `get_head_sha()` for accuracy
- No more repeated claim→skip→release churn for already-fresh reports

**3. Regression tests:**
- `test_localization_eligible_fresh_sha_skipped` — report with localization at current SHA is NOT eligible
- `test_localization_eligible_stale_sha` — updated to set `head_sha="new_sha"` to properly test stale detection
- Path traversal tests (listed above)

### Reviewer

**Action:** REQUEST_CHANGES

I verified the original blockers are addressed functionally, but there is one new blocking regression:

1. Missing DB migration for new `projects.head_sha` column breaks existing installs.
   - `head_sha` was added to `_SCHEMA` (`src/bugalizer/db.py`), and `localization_eligible_reports()` now selects `p.head_sha` (`src/bugalizer/db.py`).
   - `init_db()` only runs `CREATE TABLE IF NOT EXISTS`; it does not migrate existing tables.
   - On a DB created before this round (projects table without `head_sha`), Stage 3 eligibility now fails with:
     - `OperationalError: no such column: p.head_sha`
   - I reproduced this by creating a legacy schema DB, running current `init_db()`, then calling `localization_eligible_reports()`.
   - Requested fix:
     - Add a lightweight schema migration in `init_db()` (or equivalent startup path) that detects missing `projects.head_sha` and runs `ALTER TABLE projects ADD COLUMN head_sha TEXT`.
     - Add a regression test that simulates legacy schema -> init -> Stage 3 eligibility query, proving no `OperationalError`.

---

## Round 3

### Lead

**Action:** SUBMIT_FOR_REVIEW

Migration fix implemented. Tests expanded from 112 to 113, all passing.

**1. Schema migration in `init_db()`:**
- New `_migrate()` function in `db.py`, called by `init_db()` after `executescript(_SCHEMA)`
- Detects missing `head_sha` column via `PRAGMA table_info(projects)`
- Runs `ALTER TABLE projects ADD COLUMN head_sha TEXT` if missing
- Logs migration: `"Migration: added projects.head_sha column"`
- Idempotent: safe to call on both fresh and legacy databases

**2. Regression test:**
- `test_migration_adds_head_sha_to_legacy_schema` in `test_localizer.py`:
  - Creates an in-memory DB with the legacy schema (no `head_sha` column)
  - Verifies `head_sha` is NOT in the column list
  - Runs `_migrate(conn)`
  - Verifies `head_sha` IS now in the column list
  - Inserts a project + triaged report + triage analysis using the migrated schema
  - Calls `localization_eligible_reports()` — confirms no `OperationalError`
  - Confirms the report is correctly returned as eligible

### Reviewer

**Action:** APPROVE

Approved. The migration blocker is resolved:
- `init_db()` now runs `_migrate(conn)`, and `_migrate()` safely adds `projects.head_sha` when missing via `PRAGMA table_info(projects)` + `ALTER TABLE`.
- This addresses the legacy-DB failure mode (`no such column: p.head_sha`) identified in round 2.
- Regression coverage was added with `test_migration_adds_head_sha_to_legacy_schema`, and the targeted migration test passes locally.

---

<!-- CYCLE_STATUS -->
READY_FOR: lead
ROUND: 3
STATE: approved
