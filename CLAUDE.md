# CLAUDE.md — Bugalizer

## What This Is
AI-powered bug report processing server. Accepts structured bug reports via REST API, queues them, pre-processes with local LLMs (Ollama), optionally escalates to cloud LLMs (Anthropic), and proposes automated fixes.

## Quick Start
```bash
uv sync --dev
uv run pytest                # 185 tests, all should pass

# Run the server
BUGALIZER_DB_PATH=bugalizer.db uv run uvicorn bugalizer.main:app --port 8090
# API docs at http://localhost:8090/docs
```

## Project Structure
```
src/bugalizer/
  main.py          # FastAPI app entry point
  config.py        # Pydantic BaseSettings (env: BUGALIZER_*)
  auth.py          # API key auth (X-API-Key header)
  models.py        # Pydantic models + 13-state workflow engine
  db.py            # SQLite layer (schema + CRUD + retry_on_locked + migrations)
  api/
    reports.py     # Bug report CRUD + validation + status transitions + localization results
    projects.py    # Project CRUD + clone + repo-map endpoints
    queue.py       # Queue overview + POST retry endpoint
    usage.py       # Token usage endpoints
  llm/
    client.py      # litellm wrapper for Ollama calls
    prompts.py     # Triage + localization prompt templates
  pipeline/
    validator.py   # Stage 1: validation & pre-processing (no LLM)
    triage.py      # Stage 2: LLM triage & classification
    localizer.py   # Stage 3: two-pass LLM code localization
    repo_map.py    # AST-based repo map builder + file cache
    orchestrator.py # Pipeline coordinator with atomic claim
  git_ops/
    repo.py        # Git clone, pull, SHA, file listing via subprocess
  queue/
    worker.py      # Async background queue worker (Stages 1-3)
tests/
  test_api.py      # 30 tests: API + phase gating
  test_pipeline.py # 19 tests: validation, triage, orchestrator
  test_queue.py    # 11 tests: eligibility, retries, db locking
  test_usage.py    # 6 tests: usage endpoints, retry endpoint
  test_git_ops.py  # 15 tests: git operations
  test_repo_map.py # 11 tests: repo map builder + cache
  test_localizer.py # 21 tests: localization, eligibility, path safety, migration
```

## Architecture
- **Standalone Python/FastAPI service** with SQLite
- **Tiered LLM pipeline**: Validate (free) → Triage (Ollama) → Localize (Ollama) → Fix (Anthropic, planned)
- **13-state bug workflow** with phase gating (Phase 4 states still blocked)
- See `docs/phases/architecture.md` for full design

## Implementation Status
- **Phase 1 (Foundation): COMPLETE** — API, DB, auth, workflow, tests (30/30)
- **Phase 2 (Local LLM Pipeline): COMPLETE** — Ollama triage, async queue worker, duplicate detection, token tracking (66 tests)
- **Phase 3 (Codebase Analysis): COMPLETE** — Git ops, tree-sitter repo maps, two-pass localization, SHA freshness
- **Phase 4 (Fix Proposals): COMPLETE (codex-approved)** — Anthropic-via-litellm stage generates unified-diff fix proposals with prompt caching; `FIX_PROPOSING` transient claim state; SHA-freshness gate before paid calls; `GET /reports/{id}/fix_proposals` endpoint.
- **Phase 5 (Deployment Readiness + Dashboard): IN PROGRESS** — see `docs/phases/phase-5-deployment-readiness.md`
  - Cycle 1 (5.1 retry gates + 5.2 security defaults): COMPLETE (codex-approved)
  - Cycle 2 (5.3 per-report `analysis_mode`, `POST /reports/{id}/analyze`, per-project `fix_llm_*` provider split): IMPLEMENTED — awaiting review
  - Remaining: Cycle 3 (5.4 dashboard), Cycle 4 (5.5 deployment packaging)
- Phase 6 (Integrations): NOT STARTED

## Handoff Workflow
Uses ai-handoff system: claude (lead) ↔ codex (reviewer). Run `/handoff` to check state.

## Dev Environment
- Python 3.12.11+ with `uv`
- Queue worker disabled in tests via `BUGALIZER_QUEUE_ENABLED=false`

## Key Patterns
- Two-tier field validation: hard required (422) vs recommended (warnings array)
- Soft delete for reports (status=rejected, resolution_reason=deleted)
- Phase gating via `CURRENT_PHASE_TARGETS` in models.py
- Auth disabled when `BUGALIZER_API_KEYS` env is empty
- Atomic queue claim via `try_claim_report()` (compare-and-set on status)
- `retry_on_locked` decorator for SQLite write contention
- `db_write_lock` (asyncio.Lock) serializes worker DB writes
- `asyncio.to_thread()` wraps blocking git/AST/file ops in async worker paths
- SHA-based localization freshness: `project.head_sha` vs `localization.repo_sha`
- Per-report `analysis_mode` (auto/local_only/hold) gates automatic dispatch; manual `POST /reports/{id}/analyze` overrides it
- LLM resolution namespaces: local stages read project `llm_*`; Stage 4 reads project `fix_llm_*` → global fix settings (`resolve_local_llm`/`resolve_fix_llm` in llm/client.py)
- Path traversal protection: `_validate_candidate_path()` for LLM-provided file paths
- Schema migrations in `_migrate()` for backward-compatible column additions
- LLM calls mocked in tests (no Ollama dependency in CI)

## Claude Skills
- `/test` — Run and analyze tests (full suite or by pattern)
- `/phase` — Phase status dashboard and navigation
- `/review` — Pre-submission code review checklist
- `/pii-scan` — PII data flow audit and regulatory compliance check
- `/security-check` — OWASP-based security audit
- `/handoff` — AI handoff workflow (claude ↔ codex)
