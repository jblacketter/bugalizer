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
    documented reconciliation boundary. S0's hosted acceptance walk passed
    2026-09-30 (sonicgrid `551342c7`, Phase 46 "B1 may start"); the S0 route
    is merged (sonicgrid `e1fb2b7d`, PR #581). Runs as Phase 10 below.
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

### Phase 8: open-pr
- **Status:** Complete: B2. Codex-approved 2026-09-18 (impl round 3), merged
  2026-09-18 (PR #5, `5e7caab`). **Acceptance pending:** blocked on the
  `spherop/sonicgrid` token, which only the repo owner (Dan) can issue.
  Steps: [`docs/open-pr-acceptance.md`](open-pr-acceptance.md); BOWIE prep:
  [`docs/bowie-open-pr-prep.md`](bowie-open-pr-prep.md); report (2026-09-17):
  [`docs/bowie-open-pr-prep-report.md`](bowie-open-pr-prep-report.md).
- **Acceptance prerequisites (open, 2026-09-18):**
  1. **Token from Dan** (Part A of `open-pr-acceptance.md`) into BOWIE's
     `.env` as `BUGALIZER_GITHUB_TOKEN` (key name now fixed on BOWIE; it was
     misspelled `BUGALYZER_`). Then restart; `/health` `github_configured: true`.
  2. **A proposal whose diff applies to current sonicgrid `main`.** The only
     candidate (report `f8c0a9dbbe304f93`, sonicgrid#100) is unusable: the bug
     is already fixed upstream and the proposal edits a file that does not
     exist. Pick or file a bug that reproduces on current `main` and names a
     concrete component or file.
  3. **Stage 4 model: Greg's call.** BOWIE runs `BUGALIZER_FIX_PROVIDER=ollama`
     (`qwen2.5-coder:14b`), so "Analyze (cloud)" is local and free, and on the
     Next.js tree it invented files and libraries. A fresh bug alone may not
     fix that. Options: one paid cloud Stage 4 run (set
     `BUGALIZER_ANTHROPIC_API_KEY`, or the Phase 7 per-request key override),
     or accept several local attempts. Also worth setting the sonicgrid
     project's local `llm_model` to `qwen2.5-coder:14b` (it uses the `7b`
     default) to improve localization.
  4. Optional negative check once the token is set: run open-pr on the bad
     proposal first; expect `409 diff_does_not_apply` and no branch on GitHub.
  5. Then the walk: `open-pr-smoke.ps1 -ReportId <id>`; record the output here.
  The BOWIE sonicgrid clone (project `3e300658671b445e`) was made from the
  local checkout, because Bugalizer cannot clone a private repo (Phase 9).
  Refresh it the same way until Phase 9 lands.
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

### Phase 9: private-repo-access
- **Status:** Proposed 2026-09-18, not queued; starts on Greg's call. Should
  land before or alongside B1, which needs a clone that stays current.
- **Description:** `POST /projects/{id}/clone`, `git pull` in the pipeline and
  `refresh-map` go through `origin` with no credentials, so a private repo
  (sonicgrid) cannot be cloned or refreshed; on BOWIE it was cloned by hand
  from a local checkout. Reuse open-pr's env-only credential
  (`BUGALIZER_GITHUB_TOKEN`, `GIT_CONFIG_*` extraheader, empty
  `credential.helper`, canonical HTTPS URL, never in URL/argv/config/logs)
  for clone and fetch/pull of GitHub projects.
- **Key Deliverables (sketch):** authenticated clone and update against the
  canonical URL; token secrecy tests matching Phase 8's; a hand-made clone
  whose `origin` is SSH or a local path keeps working; no change for public
  repos without a token.
- **Open question:** the Phase 8 token is scoped to one repo. Per-project
  tokens (`credential_env` like the ingest seam) only matter if a second
  private repo appears.
- **Needs:** Phase 8's credential code; Dan's token for the acceptance run.
- **Depends on:** Phase 8

### Phase 10: sonicgrid-ingest (B1)
- **Status:** Complete: codex-approved 2026-09-30 (impl round 2; plan round 2), merged
  (PR #6, setup script PR #7), live on BOWIE 2026-09-30: bugs filed in sonicgrid appear
  on the BOWIE dashboard (acceptance walked by Jack; `docs/sonicgrid-ingest-acceptance.md`).
  (`docs/phases/sonicgrid-ingest.md`). Acceptance: `docs/sonicgrid-ingest-acceptance.md`
  on BOWIE after merge.
  Unblocked: sonicgrid S0 hosted acceptance PASS 2026-09-30. Lands before
  Phase 9 (arbiter, 2026-09-29); imported reports localize against the
  hand-refreshed BOWIE clone until Phase 9.
- **Description:** Pull sonicgrid bug reports into Bugalizer's queue. Reporters keep
  using sonicgrid's Report Bug dialog; a background poller on BOWIE reads S0's
  `GET /api/bugalizer/bug-reports` with `SONICGRID_POLL_TOKEN`, imports each report
  once (idempotent by sonicgrid id), and the normal pipeline triages it. Nothing
  is written back to sonicgrid.
- **Key Deliverables:**
  - Ingest poller task (`BUGALIZER_INGEST_ENABLED`, off by default) with the
    contract's cursor rules and a periodic full re-walk for reconciliation
  - `bug_reports.external_id` + partial unique index; `ingest_state` checkpoint table
  - `GET /projects/{id}/ingest`, `POST /projects/{id}/ingest/run`; `/health` ingest block
  - Token secrecy tests matching Phases 7 and 8; reporter email never stored
- **Depends on:** Phase 7

### Phase 11: sonicgrid-triage-sync
- **Status:** B3, impl codex-approved 2026-10-02 (impl round 3; plan round 3;
  `docs/phases/sonicgrid-triage-sync.md`). PR #10 merged before review; the r1/r2 fixes
  are on branch `phase-11/review-fixes` (merge that before enabling the sync on BOWIE). Acceptance on BOWIE after merge:
  `docs/sonicgrid-triage-acceptance.md`. Direction:
  `~/projects/QA/docs/bugalizer-sonicgrid-triage-direction-2026-09-30.html`
  (rulings E1 to E6, Jack; first users Jack and Dan). Pairs with
  sonicgrid's S2 `sonicgrid-triage`, whose contract document it builds against.
- **Description:** Make Bugalizer visible and actionable from sonicgrid without
  exposing BOWIE: push each report's results to sonicgrid (reporters see status and
  summary; admins also see root cause, files and diff), and pull admin action requests
  from it (analyze local/cloud, "fix and open PR", open PR, mode, close). A human
  reviews and merges every PR on GitHub.
- **Key Deliverables (sketch):** results push with watermark and retry; action pull
  (about 15 s) idempotent by action id; "fix and open PR" chained behind two consents,
  stopping at the first refusal; cloud-tier actions only for a BOWIE-side allowlist of
  sonicgrid users (initially Jack), everyone else on BOWIE's local models; spend
  attributed per requesting user via `key_ref`; a second, write-scoped sonicgrid token
  resolved like `SONICGRID_POLL_TOKEN`.
- **Needs (outside this repo):** sonicgrid S2's contract document: landed as
  `documentation/BUGALIZER-TRIAGE-ENDPOINTS.md` (sonicgrid `9ce7fe50`, PR #618), hosted acceptance 2026-09-30.
- **Depends on:** Phase 10, Phase 8

### Phase 12: per-user-cloud-keys
- **Status:** B4, plan 2026-10-03 (`docs/phases/per-user-cloud-keys.md`, cross-repo; decisions
  F1 to F7, Jack). Pairs with sonicgrid's S3 `sonicgrid-ai-settings` and S4 `sonicgrid-bug-board`.
- **Description:** Nobody spends another person's cloud key (E4, F3). Each sonicgrid admin
  saves their own Claude key and default model on a sonicgrid AI settings page (like the
  Aegis AI settings); Bugalizer fetches the requester's key once per paid action and uses it
  for that call only through Phase 7's per-request override, never storing or logging it.
  The cloud allowlist is retired. Bugalizer also pushes a six-lane `stage` for sonicgrid's
  new board, and reads fix PRs' state on GitHub so a merged PR completes the bug.
- **Depends on:** Phase 11, Phase 8

## Decision Log
See `docs/decision_log.md`

## Getting Started
1. Use `/phase` to check current phase
2. Use `/tagteam:handoff` to check the review cycle state
3. See `README.md` for install/run instructions
