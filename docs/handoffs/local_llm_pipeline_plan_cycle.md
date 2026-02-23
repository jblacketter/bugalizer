# Handoff Cycle: local_llm_pipeline (Plan Review)

- **Phase:** local_llm_pipeline
- **Type:** plan
- **Date:** 2026-02-22
- **Lead:** claude
- **Reviewer:** codex

## References
- Phase plan: `docs/phases/local_llm_pipeline.md`
- Architecture plan: `docs/phases/architecture.md`

---

## Round 1

### Lead

**Action:** SUBMIT_FOR_REVIEW

Phase 2 (Local LLM Pipeline) plan is ready for review at `docs/phases/local_llm_pipeline.md`.

**What this phase delivers:**

1. **Two-stage automated pipeline:**
   - Stage 1 (Validation): Pure Python, zero LLM cost — extracts structured data (URLs, file paths, error messages, stack traces), detects duplicates via text similarity (difflib SequenceMatcher, configurable threshold), auto-transitions `submitted` → `validating` → `triaged` (or `rejected`/`duplicate`)
   - Stage 2 (Triage): Ollama via litellm — severity classification, category assignment, feature area ID, structured summary, clarification detection. Returns structured JSON. Auto-transitions `triaged` → `analyzing` → enriched `triaged` (or `clarification_needed`)

2. **Async queue worker:**
   - asyncio background task polling DB for processable reports
   - Configurable concurrency (semaphore, default 2) and poll interval (default 5s)
   - Integrates with FastAPI lifespan for clean startup/shutdown
   - Handles LLM failures gracefully (report stays queued for retry)

3. **Token usage tracking:**
   - Every LLM call logged in `analyses` table (prompt/completion tokens, cost)
   - Aggregated in `token_usage` table per project
   - New endpoints: `GET /api/v1/usage` and `GET /api/v1/usage/{project_id}`

4. **Phase gating update:**
   - Unlocks `analyzing` and `clarification_needed` transitions
   - Keeps Phase 3/4 states gated (`fix_proposed`, `fix_approved`, `fix_committed`, `verified`)
   - Renames `PHASE1_ALLOWED_TARGETS` → phase-generic naming

5. **New dependencies:** litellm moved from optional `[llm]` to core deps

6. **13 new/modified files**, 3 new test files, all existing tests maintained

**Key design decisions:**
- Duplicate detection uses difflib (simple, no external deps) rather than embedding-based similarity — sufficient for Phase 2, can upgrade in Phase 3 when we have repo maps
- Triage prompts request structured JSON output — compact, parseable, minimal token waste
- Queue worker is in-process asyncio (not Celery/Redis) — matches architecture plan's "start simple" approach
- LLM client is a thin litellm wrapper — reuses the qaagent pattern, easy to swap models

### Reviewer

**Action:** REQUEST_CHANGES

Strong plan overall and aligned with the architecture phase. Before approval, please address these blocking gaps:

1. Clarify retry eligibility for Stage 2 triage failures.
   - The plan says triage failures should stay queued for retry, but worker selection is described as `triaged` reports with "no prior triage analysis."
   - That condition would skip reports that already have a failed triage analysis, preventing retries.
   - Please define explicit retry selection rules (for example: no successful triage analysis yet, or latest triage analysis failed and retry window elapsed).

2. Add an explicit queue-claim/idempotency strategy to prevent double-processing.
   - With concurrent workers, polling by status alone can race and schedule the same report more than once.
   - Please specify how a report is atomically claimed for each stage (for example, compare-and-set status transition `submitted -> validating` and `triaged -> analyzing` before processing, and skip if claim fails).

3. Define SQLite concurrency handling for background processing.
   - Current codebase uses a shared synchronous sqlite layer. Introducing concurrent async worker tasks adds write-contention risk (`database is locked`) under load.
   - Please add a concrete Phase 2 approach (for example, single-writer section, bounded retry/backoff on sqlite OperationalError, or serialized DB writes in worker path) so implementation behavior is predictable.

## Round 2

### Lead

**Action:** SUBMIT_FOR_REVIEW

All 3 reviewer items addressed in `docs/phases/local_llm_pipeline.md`:

**1. Retry eligibility for Stage 2 triage failures — fully specified:**
- Added "Retry Rules for Failed Triage" section to the plan
- A triaged report is eligible for Stage 2 when: (a) no triage analysis exists, OR (b) most recent triage has `status='failed'` and `completed_at` is older than `BUGALIZER_RETRY_DELAY_SECONDS` (default: 60s)
- Not eligible when: successful triage exists, or last failure is within retry delay window
- Max retries capped at `BUGALIZER_MAX_TRIAGE_RETRIES` (default: 3) — after that, report is skipped by worker but stays `triaged` for manual intervention via `POST /api/v1/queue/{id}/retry`
- Failed triage analyses are saved with `status='failed'` (not silently discarded)
- Two new config settings added: `retry_delay_seconds`, `max_triage_retries`

**2. Queue claim & idempotency strategy — atomic compare-and-set:**
- Added "Queue Claim & Idempotency Strategy" section
- Stage 1 claim: atomic `UPDATE ... SET status='validating' WHERE id=? AND status='submitted'`, check `rowcount`
- Stage 2 claim: same pattern with `triaged → analyzing`
- New DB function: `try_claim_report(report_id, expected_status, new_status) -> bool`
- On pipeline failure after claim: orchestrator rolls status back to pre-claim state and records failed analysis
- Guarantees exactly-once processing per pipeline invocation

**3. SQLite concurrency strategy — defined and bounded:**
- Added "SQLite Concurrency Strategy" section
- All DB writes from queue worker path serialized via `asyncio.Lock` (`db_write_lock`) — async tasks in same event loop, so no thread blocking
- Retry on `sqlite3.OperationalError` up to 3 times with exponential backoff (0.1s, 0.2s, 0.4s) for edge cases where API handler writes concurrently
- WAL mode already enabled, sufficient for expected load
- Single shared connection kept for Phase 2; connection pooling or PostgreSQL deferred to Phase 5 if needed

### Reviewer

**Action:** REQUEST_CHANGES

Most of the prior blockers are addressed well. One blocking inconsistency remains:

1. Stage 2 worker eligibility is still contradictory in the plan.
   - `In Scope` under Async queue worker still says Stage 2 picks up `triaged` reports with **"no prior triage analysis"**.
   - The new retry rules correctly allow retries after failed triage analyses (subject to backoff and max retries).
   - Please align all sections to the same rule so implementation does not accidentally regress to first-attempt-only behavior.

Optional clarity improvement (non-blocking but recommended in same edit):
- The plan references manual `POST /api/v1/queue/{id}/retry` behavior. If that endpoint is in Phase 2 scope, add it explicitly to scope/files/success criteria; if not, remove that reference.

## Round 3

### Lead

**Action:** SUBMIT_FOR_REVIEW

Both items addressed in `docs/phases/local_llm_pipeline.md`:

**1. Stage 2 worker eligibility — aligned across all sections:**
- In Scope now reads: "Picks up `triaged` reports eligible for Stage 2 (no successful triage, or last triage failed and past retry delay; max retries not exceeded)"
- This matches the retry rules in the Technical Approach section exactly
- No more contradictory "no prior triage analysis" language anywhere in the plan

**2. `POST /api/v1/queue/{id}/retry` — explicitly added to scope, files, and success criteria:**
- Added to In Scope as a bullet: "Manual retry endpoint — resets retry count for a report"
- Added `src/bugalizer/api/queue.py` to Modified Files table
- Added success criteria #8: "`POST /api/v1/queue/{id}/retry` resets retry count and makes a report eligible for re-processing"
- Added "retry endpoint" to test coverage in success criteria #10

### Reviewer

**Action:** APPROVE

Approved. The remaining inconsistency is resolved:
- Stage 2 worker eligibility language is now aligned across Scope, Queue Worker, and Retry Rules (including failed-attempt retries with delay/max limits)
- `POST /api/v1/queue/{id}/retry` is now explicitly included in scope/files/success criteria, so manual retry behavior is no longer ambiguous

---

<!-- CYCLE_STATUS -->
READY_FOR: lead
ROUND: 3
STATE: approved
