# CLAUDE.md — Bugalizer

## What This Is
AI-powered bug report processing server. Accepts structured bug reports via REST API, queues them, pre-processes with local LLMs (Ollama), optionally escalates to cloud LLMs (Anthropic), and proposes automated fixes.

## Quick Start
```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest                       # 30 tests, all should pass

# Run the server
BUGALIZER_DB_PATH=bugalizer.db uvicorn bugalizer.main:app --port 8090
# API docs at http://localhost:8090/docs
```

## Project Structure
```
src/bugalizer/
  main.py          # FastAPI app entry point
  config.py        # Pydantic BaseSettings (env: BUGALIZER_*)
  auth.py          # API key auth (X-API-Key header)
  models.py        # Pydantic models + 13-state workflow engine
  db.py            # SQLite layer (schema + CRUD)
  api/
    reports.py     # Bug report CRUD + two-tier validation + status transitions
    projects.py    # Project CRUD
    queue.py       # Queue overview (counts by status)
tests/
  test_api.py      # 30 tests covering all Phase 1 functionality
docs/
  phases/architecture.md              # Full architecture plan (approved)
  handoffs/architecture_plan_cycle.md # Plan review (approved, 3 rounds)
  handoffs/architecture_impl_cycle.md # Impl review (approved, 3 rounds)
```

## Architecture
- **Standalone Python/FastAPI service** with SQLite
- **Tiered LLM pipeline** (planned): Validate (free) → Triage (Ollama) → Localize (Ollama) → Fix (Anthropic)
- **13-state bug workflow** with Phase 1 gating (AI states blocked until later phases)
- See `docs/phases/architecture.md` for full design

## Implementation Status
- **Phase 1 (Foundation): COMPLETE** — API, DB, auth, workflow, tests (30/30)
- Phase 2 (Local LLM Pipeline): NOT STARTED
- Phase 3 (Codebase Analysis): NOT STARTED
- Phase 4 (Fix Proposals): NOT STARTED
- Phase 5 (Dashboard): NOT STARTED
- Phase 6 (Integrations): NOT STARTED

## Handoff Workflow
Uses ai-handoff system: claude (lead) ↔ codex (reviewer). Run `/handoff` to check state.

## Dev Environment
- Develop on macOS (current). Production target: Windows with RTX 4070 Super for Ollama.
- GPU-dependent work (Ollama integration) saved for Windows deployment.

## Key Patterns
- Two-tier field validation: hard required (422) vs recommended (warnings array)
- Soft delete for reports (status=rejected, resolution_reason=deleted)
- Phase 1 transition gating via `PHASE1_ALLOWED_TARGETS` in models.py
- Auth disabled when `BUGALIZER_API_KEYS` env is empty
