# Handoff Cycle: architecture (Plan Review)

- **Phase:** architecture
- **Type:** plan
- **Date:** 2026-02-22
- **Lead:** claude
- **Reviewer:** codex

## References
- Phase plan: `docs/phases/architecture.md`

---

## Round 1

### Lead

**Action:** SUBMIT_FOR_REVIEW

The architecture plan for Bugalizer is ready for review. Key decisions and their rationale:

**1. Separate Service (not embedded in qaagent)**
- qaagent is a mature QA automation framework; Bugalizer has a distinct domain (bug lifecycle + AI fix proposals)
- Standalone service accepts reports from any source (sonicgrid, manual, future apps)
- Reuses qaagent's litellm pattern for LLM integration without direct dependency
- Shares the same Windows machine and Ollama instance

**2. Agentless-Inspired Tiered Pipeline**
- Research shows Agentless (3-phase: Localize → Repair → Validate) achieves 50.8% success at ~$0.34/issue
- Our pipeline: Validate (free) → Triage (Ollama, free) → Localize (Ollama, free) → Fix Proposal (Anthropic, ~$0.04-0.15)
- Most processing happens on local GPU at zero marginal cost
- Cloud LLM only used for confident fix generation

**3. Bug Status Workflow**
- Research-informed 11-state workflow covering the full lifecycle
- Based on industry practices (Jira, Azure DevOps, bug lifecycle standards)
- Includes AI-specific states: `analyzing`, `clarification_needed`, `fix_proposed`

**4. Token Efficiency Strategy**
- Aider-inspired repo maps (graph-ranked file importance, ~1-4k tokens)
- Pre-filter with regex/AST before any LLM call
- Focused context: only localized files sent to cloud LLM
- Caching: repo maps rebuilt only on git changes

**5. Tech Stack**
- Python/FastAPI (matches qaagent, enables code sharing)
- SQLite (simple, no external deps)
- Asyncio queue (start simple, upgrade later if needed)
- tree-sitter for multi-language AST parsing

**6. Phased Implementation**
- 6 phases from foundation through integrations
- Phase 1 (foundation) delivers a working API immediately
- Each phase is independently valuable

Please review the architecture, status workflow, pipeline design, and database schema for completeness and correctness.

### Reviewer

**Action:** REQUEST_CHANGES

Good overall direction and structure. Before approval, please address these items in `docs/phases/architecture.md`:

1. Clarify scope boundaries for Phase 1 vs later phases.
   - The plan says "Phase 1 (foundation) delivers a working API immediately", but queue processing, workflow transitions, and pipeline orchestration are split across later phases.
   - Add a concrete "what is fully working in Phase 1" list (exact endpoints, persisted fields, and which status transitions are actually executable in code).

2. Resolve status-model inconsistencies.
   - The document calls this an "11-state workflow", but the flow currently includes more states (`submitted`, `validating`, `rejected`, `triaged`, `analyzing`, `clarification_needed`, `fix_proposed`, `fix_approved`, `fix_committed`, `verified`, `closed`, `fix_rejected`, `deferred`, `duplicate`, `wont_fix`).
   - Either reduce to a canonical set or explicitly define the full canonical list and terminal states.

3. Tighten schema requirements alignment.
   - The required bug-report fields include `expected_behavior` and `actual_behavior`, but `bug_reports` table defines them as nullable.
   - Specify whether these are truly required at API+DB level now, or intentionally optional in initial ingestion with later clarification.

4. Define the security model for credentials/secrets.
   - `api_key_encrypted` is listed, but there is no key management strategy (where encryption key lives, rotation policy, local dev behavior).
   - Add a minimal concrete approach so implementation is not blocked by ambiguous security requirements.

5. Specify repo-map rebuild and invalidation rules.
   - "Rebuild on git changes only" needs an operational definition (commit hash, branch head, file mtime snapshot, or webhook event trigger).
   - Add deterministic cache key/invalidation criteria to avoid stale localization context.

## Round 2

### Lead

**Action:** SUBMIT_FOR_REVIEW

All 5 reviewer items addressed in `docs/phases/architecture.md`:

**1. Phase 1 scope clarified** — Added a detailed "What is fully working at the end of Phase 1" section listing:
- 10 exact endpoints delivered
- All persisted fields per table
- 7 specific status transitions executable in Phase 1 (all human/manual)
- Explicit "NOT in Phase 1" list (no LLM, no async processing, no git ops, no dashboard, no webhooks)
- Auth model: API key via header only, no JWT

**2. Status workflow canonicalized** — Replaced vague "11-state" description with:
- Canonical table of 13 states with owner, description, and terminal flag
- 3 terminal states explicitly marked: `closed`, `rejected`, `duplicate`
- `deferred` clarified as non-terminal (can return to `triaged`)
- Removed `fix_rejected` and `wont_fix` (consolidated: `fix_rejected` → `triaged` or `closed`; `wont_fix` → `closed`)
- Full transition rules table with every valid source → target pair

**3. Schema/DB alignment fixed** — Introduced two-tier validation:
- **Hard required** (4 fields: `title`, `description`, `reporter`, `project_id`): 422 on missing, `NOT NULL` in DB
- **Recommended** (3 fields: `steps_to_reproduce`, `expected_behavior`, `actual_behavior`): API accepts but returns `warnings` array with guidance. DB columns nullable. AI triage may escalate to `clarification_needed` if these plus description are insufficient.
- **Optional**: No validation, nullable

**4. Security model defined** — New "Security Model for Credentials" section:
- Bugalizer API keys: env var only, no DB
- LLM keys: Fernet symmetric encryption in SQLite, encryption key in `BUGALIZER_SECRET_KEY` env var
- Key rotation CLI command specified
- Fallback to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` env vars (qaagent pattern)
- Ollama: no auth needed, configurable host
- Git credentials: system-level only, Bugalizer stores nothing

**5. Repo map invalidation defined** — New "Repo Map Cache Invalidation" section:
- Cache key: `{project_id}:{branch}:{HEAD_commit_sha}`
- File-based cache storage path specified
- 4 invalidation triggers: on submit (SHA check), explicit refresh endpoint, optional periodic fetch, 24h TTL safety net
- Build cost noted: pure AST, no LLM, <5s for 10k files

### Reviewer

**Action:** REQUEST_CHANGES

Most of the requested updates were completed well. One blocking inconsistency remains:

1. Canonical state model still conflicts with Phase 1 transition examples.
   - In the canonical status set, `wont_fix` was removed and consolidated into `closed`.
   - But the Phase 1 executable transitions still include: `triaged` → `... / wont_fix`.
   - Please update Phase 1 transitions to use only canonical states (e.g., `triaged` → `closed` with reason metadata such as `resolution_reason = wont_fix`) and ensure the same transition vocabulary is used consistently across:
     - Canonical Status Set
     - Transition Rules
     - Phase 1 executable transitions
     - Any endpoint/status examples

Once this is aligned, I expect to approve.

---

## Round 3

### Lead

**Action:** SUBMIT_FOR_REVIEW

Fixed the one remaining inconsistency:

**Phase 1 transitions aligned to canonical states:**
- Changed `triaged → ... / wont_fix` to `triaged → ... / closed` with `resolution_reason` metadata
- Added `resolution_reason TEXT` column to `bug_reports` DB schema (set when entering terminal states, e.g. `"fixed"`, `"wont_fix"`, `"not_a_bug"`, `"cannot_reproduce"`)
- Verified: `wont_fix` and `fix_rejected` no longer appear as status values anywhere in the document — only as `resolution_reason` examples
- All three vocabulary locations now use the same 13 canonical states: Canonical Status Set table, Transition Rules, and Phase 1 executable transitions

### Reviewer

**Action:** APPROVE

Approved. The remaining inconsistency was resolved:
- Phase 1 transitions now use canonical statuses only (`closed` instead of `wont_fix` state)
- `wont_fix` is now represented as `resolution_reason` metadata, which aligns with the canonical state model
- Transition vocabulary is now consistent across canonical states, transition rules, and Phase 1 examples

---

<!-- CYCLE_STATUS -->
READY_FOR: lead
ROUND: 3
STATE: approved
