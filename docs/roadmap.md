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
- **Status:** Complete (impl approved by codex 2026-07-03, `docs/handoffs/phase-4-fix-proposals_impl_rounds.jsonl`; `docs/phases/phase-4-fix-proposals.md`)
- **Description:** Cloud-LLM (Anthropic via litellm) unified-diff fix proposals.
- **Key Deliverables:**
  - Stage 4 fix proposer with prompt caching and size-capped file bundles
  - `FIX_PROPOSING` claim state; `GET /reports/{id}/fix_proposals`; `QA_LLM_*` fallback layer

### Phase 5: Deployment Readiness & Queue Dashboard
- **Status:** Complete — hosting milestone closed 2026-07-02 (`docs/phases/phase-5-deployment-readiness.md`)
- **Description:** Make the service safe to host permanently on the LAN, with a queue
  dashboard and per-report local-vs-cloud analysis choice.
- **Key Deliverables:**
  - Stage 3/4 retry caps + real health check; security defaults (keys, CORS)
  - Per-report analysis tier (`local` / `cloud`); minimal web dashboard
  - Docker/service packaging + Windows LAN deploy guide — live at `https://bugalizer.lan/`

### Phase 5b: Dashboard UX & Tier Clarity
- **Status:** Complete — both impl cycles codex-approved 2026-07-03 (`docs/phases/phase-5b-dashboard-ux.md`); Windows redeploy pending
- **Description:** Make the dashboard a genuinely good operator UI: unmistakable local-vs-cloud
  tier identity (free vs paid), visible "already scanned" state, readable results, and no
  curl-only workflows. Fake bug reports as working material.
- **Key Deliverables:**
  - Aegis "Control Room" design language (Slate/Mist themes, steel/emerald/violet tier chips)
  - Formatted triage + clarification questions, colorized diffs, run-history timeline
  - Health strip, filters, project management UI, bug submission form

### Phase 6: Integrations
- **Status:** Not Started. Shaped by the Aegis direction record
  `~/projects/QA/docs/bugalizer-integration-direction-2026-09-12.html` (rulings
  D1 to D6, Greg 2026-09-12). Runs as two phases in this repo after Phase 7,
  each on Greg's call.
- **Description:** Connect Bugalizer to sonicgrid and to Aegis.
- **Key Deliverables:**
  - B1 `sonicgrid-ingest`: poller for sonicgrid bug reports, idempotent by id.
    Depends on Phase 7 and sonicgrid S0.
    **Rescoped 2026-09-15 — S0 shipped a different surface than D2 assumed.**
    Sonicgrid runs on Vercel and cannot reach BOWIE, so it built an
    authenticated pull endpoint instead of the scoped Supabase role:
    `GET /api/bugalizer/bug-reports?cursor=&limit=` behind a static bearer
    token, returning `{reports[], next_cursor}` over a composite
    `(created_at, id)` cursor. B1 passes the cursor back verbatim and stores
    it only when non-null. The write-back columns, response mapping and
    sonicgrid admin column named for S0 are **deferred, not planned** — so B1
    writes nothing back and the Aegis exit line "id and status appear on the
    sonicgrid admin bugs page" no longer applies (D5 carry, Greg's, still
    owed to the Aegis direction record). Contract:
    `documentation/BUGALIZER-POLL-ENDPOINT.md` in the sonicgrid repo; fits
    B0's `{url, table, credential_env}` with no Bugalizer schema change.
    Late inserts and reopened reports can sort behind a checkpoint — B1's
    documented reconciliation boundary. Blocked until sonicgrid records S0's
    hosted acceptance walk (sonicgrid `docs/roadmap.md`, Phase 46); the S0
    route itself is merged (sonicgrid `e1fb2b7d`, PR #581).
  - B2 `open-pr`: apply the proposed diff on `fix/bugalizer-<report-id>`,
    commit, push, open a PR with the analysis as the body; unlock
    fix_approved and fix_committed; write policy tested (never main, never
    force, one PR per report). The only place that writes to a remote.
    Depends on Phase 7. Runs as Phase 8 below.
- **Depends on:** Phase 7

### Phase 7: bugalizer-revive (B0)
- **Status:** Complete — impl approved by codex 2026-09-12 (round 3, PR #2 at 7b73c30,
  `docs/handoffs/bugalizer-revive_impl_rounds.jsonl`); PR #2 merged, and
  `scripts/windows/check-service.ps1` reported VERIFIED on BOWIE
  (`docs/phases/bugalizer-revive.md`). Follow-up PR #3 fixed the service-name match.
- **Description:** Bring the repo back under tagteam (clean tree, CI, docs
  that match the suite), add the per-request provider/model/key override on
  the cloud analyze call (D3) and the per-project ingest-source setting B1
  reads, report auth state on `/health`, ship the BOWIE service check as a
  script. First phase of the Aegis arc.
- **Key Deliverables:**
  - `.github/workflows/ci.yml` running pytest
  - `POST /reports/{id}/analyze` optional `llm` override, never stored
  - `projects.ingest_source` / `ingest_config`
  - `scripts/windows/check-service.ps1`
- **Depends on:** Phase 5b

### Phase 8: open-pr (B2)
- **Status:** Implemented, codex-approved 2026-09-18 (impl round 3), merged
  2026-09-18 (PR #5, `5e7caab`). **Acceptance pending:** blocked on the
  `spherop/sonicgrid` token, which only the repo owner (Dan) can issue.
  Steps: [`docs/open-pr-acceptance.md`](open-pr-acceptance.md); BOWIE prep:
  [`docs/bowie-open-pr-prep.md`](bowie-open-pr-prep.md); report (2026-09-17):
  [`docs/bowie-open-pr-prep-report.md`](bowie-open-pr-prep-report.md).
- **Description:** Turn a `fix_proposed` report into a pull request on the
  project's GitHub repo: apply the stored diff in a throwaway worktree on
  `fix/bugalizer-<report-id>`, commit, push, open the PR with the analysis as
  the body. Unlocks `fix_approved` and `fix_committed` for this path only. The
  only code in Bugalizer that writes to a remote; the Aegis button is A2.
- **Key Deliverables:**
  - `POST /reports/{id}/open-pr`, idempotent per report (second call returns the
    existing PR)
  - Write policy enforced by tests: never the default branch, never force, one
    branch and one PR per report, never merge
  - `BUGALIZER_GITHUB_TOKEN` (fine-grained, single repo) that never reaches a
    URL, argv, git config, log or response
  - `scripts/windows/open-pr-smoke.ps1` for the BOWIE acceptance walk
- **Depends on:** Phase 7

## Decision Log
See `docs/decision_log.md`

## Getting Started
1. Use `/phase` to check current phase
2. Use `/tagteam:handoff` to check the review cycle state
3. See `README.md` for install/run instructions
