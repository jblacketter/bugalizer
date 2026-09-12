# CLAUDE.md — Bugalizer

## What This Is
AI-powered bug report processing server. Accepts structured bug reports via REST API, queues them, pre-processes with local LLMs (Ollama), optionally escalates to cloud LLMs (Anthropic), and proposes automated fixes.

## Quick Start
```bash
uv sync --dev
uv run pytest                # the full suite, all pass; LLM calls are mocked

# Run the server
BUGALIZER_DB_PATH=bugalizer.db uv run uvicorn bugalizer.main:app --port 8090
# API docs at http://localhost:8090/docs
```

## Project Structure
```
src/bugalizer/
  main.py          # FastAPI app entry point (serves dashboard at /, assets at /static)
  config.py        # Pydantic BaseSettings (env: BUGALIZER_*)
  static/
    index.html     # Queue dashboard shell (Control Room design, Slate/Mist themes)
    styles.css     # Design system: tier colors (emerald=local/free, violet=cloud/paid)
    app.js         # Board + drawer + modals (vanilla JS, 5s fetch-polling)
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
  test_api.py      # API + phase gating, health, projects (incl. ingest fields), validation secrecy
  test_analysis_mode.py # analysis_mode gating, manual analyze endpoint, per-request LLM override
  test_pipeline.py # validation, triage, orchestrator
  test_queue.py    # eligibility, retries, db locking
  test_usage.py    # usage endpoints (incl. key_source/key_ref attribution), retry endpoint
  test_git_ops.py  # git operations
  test_repo_map.py # repo map builder + cache
  test_localizer.py # localization, eligibility, path safety, migration
  test_fix_proposer.py # Stage 4: proposals, retry classification, override + key secrecy
```

## Architecture
- **Standalone Python/FastAPI service** with SQLite
- **Tiered LLM pipeline**: Validate (free) → Triage (Ollama) → Localize (Ollama) → Fix (Anthropic, planned)
- **13-state bug workflow** with phase gating (Phase 4 states still blocked)
- See `docs/phases/architecture.md` for full design

## Implementation Status
- **Phase 1 (Foundation): COMPLETE**: API, DB, auth, workflow, tests
- **Phase 2 (Local LLM Pipeline): COMPLETE**: Ollama triage, async queue worker, duplicate detection, token tracking
- **Phase 3 (Codebase Analysis): COMPLETE** — Git ops, tree-sitter repo maps, two-pass localization, SHA freshness
- **Phase 4 (Fix Proposals): COMPLETE (codex-approved)** — Anthropic-via-litellm stage generates unified-diff fix proposals with prompt caching; `FIX_PROPOSING` transient claim state; SHA-freshness gate before paid calls; `GET /reports/{id}/fix_proposals` endpoint.
- **Phase 5 (Deployment Readiness + Dashboard): COMPLETE** — all 4 cycles codex-approved;
  hosting milestone closed 2026-07-02 (live at `https://bugalizer.lan/` on the Windows GPU box)
- **Phase 5b (Dashboard UX & Tier Clarity): IN PROGRESS** — see `docs/phases/phase-5b-dashboard-ux.md`
  - Cycle 1 (5b.1 tier identity + 5b.2 readable results): COMPLETE (codex-approved) — static split
    to `index.html`/`styles.css`/`app.js`, Slate/Mist themes, tier scan-state chips, formatted
    triage, colorized diffs, run-history timeline; per-thread SQLite connections crash fix
  - Cycle 2 (5b.3 ops visibility + 5b.4 UI workflows): IMPLEMENTED — health LEDs, filters,
    terminal collapse, project management modal, bug submission form, retry-only-when-failed
- Phase 6 (Integrations): NOT STARTED

## Handoff Workflow
Uses tagteam: claude (lead) ↔ codex (reviewer). Read `tagteam.yaml` and `handoff-state.json`,
then follow the handoff contract: `/tagteam:handoff` (Claude Code plugin) or `tagteam contract`.
See `AGENTS.md` and `docs/workflows.md`. The old vendored `.claude/skills/handoff/` is gone;
the plugin serves the skill.

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
- `/tagteam:handoff` — AI handoff workflow (claude ↔ codex), served by the tagteam plugin
