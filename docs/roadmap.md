# Project Roadmap

## Overview
Bugalizer is an AI-powered bug report processing server: structured bug reports come in over
REST, queue through a tiered LLM pipeline (validate → local-LLM triage → local-LLM code
localization → cloud-LLM fix proposals), and accumulate enrichment toward reviewed, applied
fixes. Full design: `docs/phases/architecture.md`.

**Tech Stack:** Python 3.12 / FastAPI / SQLite (WAL) / litellm (Ollama + Anthropic) /
tree-sitter / uv

**Workflow:** Lead (claude) / Reviewer (codex) with Human Arbiter via tagteam
(see `tagteam.yaml`)

## Phases

### Phase 1: Foundation
- **Status:** Complete (approved)
- **Description:** REST API, SQLite layer, API-key auth, 13-state workflow with phase gating.
- **Key Deliverables:**
  - Reports/projects/queue CRUD endpoints with two-tier field validation
  - Workflow engine with enforced transitions (`CURRENT_PHASE_TARGETS`)

### Phase 2: Local LLM Pipeline
- **Status:** Complete
- **Description:** Ollama-backed triage with an async background queue worker.
- **Key Deliverables:**
  - Stage 1 validation + duplicate detection; Stage 2 triage with retry caps
  - Queue worker (poll loop, semaphore-bounded, atomic claims); token usage tracking

### Phase 3: Codebase Analysis
- **Status:** Complete
- **Description:** Git-aware, AST-based code localization.
- **Key Deliverables:**
  - Git clone/pull/SHA ops; tree-sitter repo maps with SHA-based cache invalidation
  - Two-pass localization with confidence threshold and path-traversal protection

### Phase 4: Fix Proposals
- **Status:** Implemented — impl review in progress (`docs/phases/phase-4-fix-proposals.md`)
- **Description:** Cloud-LLM (Anthropic via litellm) unified-diff fix proposals.
- **Key Deliverables:**
  - Stage 4 fix proposer with prompt caching and size-capped file bundles
  - `FIX_PROPOSING` claim state; `GET /reports/{id}/fix_proposals`; `QA_LLM_*` fallback layer

### Phase 5: Deployment Readiness & Queue Dashboard
- **Status:** Planned (`docs/phases/phase-5-deployment-readiness.md`)
- **Description:** Make the service safe to host permanently on the LAN, with a queue
  dashboard and per-report local-vs-cloud analysis choice.
- **Key Deliverables:**
  - Stage 3/4 retry caps + real health check; security defaults (keys, CORS)
  - Per-report analysis tier (`local` / `cloud`); minimal web dashboard
  - Docker/service packaging + Windows LAN deploy guide

### Phase 6: Integrations
- **Status:** Not Started
- **Description:** Connect Bugalizer to the outside world.
- **Key Deliverables:**
  - Webhooks / external bug tracker ingestion (sonicgrid, qaagent)

## Decision Log
See `docs/decision_log.md`

## Getting Started
1. Use `/phase` to check current phase
2. Use `/handoff` to check the review cycle state
3. See `README.md` for install/run instructions
