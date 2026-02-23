# Handoff Cycle: local_llm_pipeline (Implementation Review)

- **Phase:** local_llm_pipeline
- **Type:** impl
- **Date:** 2026-02-22
- **Lead:** claude
- **Reviewer:** codex

## References
- Phase plan: `docs/phases/local_llm_pipeline.md`
- Approved plan review: `docs/handoffs/local_llm_pipeline_plan_cycle.md`

### Implementation Files Created
1. `src/bugalizer/llm/__init__.py` — Package init
2. `src/bugalizer/llm/client.py` — litellm wrapper for Ollama calls (async, structured response)
3. `src/bugalizer/llm/prompts.py` — Triage prompt templates with JSON output format
4. `src/bugalizer/pipeline/__init__.py` — Package init
5. `src/bugalizer/pipeline/validator.py` — Stage 1: structured data extraction, duplicate detection
6. `src/bugalizer/pipeline/triage.py` — Stage 2: LLM triage with analysis/usage tracking
7. `src/bugalizer/pipeline/orchestrator.py` — Pipeline coordinator with atomic claim and rollback
8. `src/bugalizer/queue/__init__.py` — Package init
9. `src/bugalizer/queue/worker.py` — Async background queue worker with semaphore concurrency
10. `src/bugalizer/api/usage.py` — Token usage endpoints (GET /usage, GET /usage/{project_id})
11. `tests/test_pipeline.py` — 19 tests: extraction, duplicates, validation, triage, orchestrator
12. `tests/test_queue.py` — 7 tests: eligibility, retry delay, max retries, reset
13. `tests/test_usage.py` — 6 tests: usage endpoints, retry endpoint

### Implementation Files Modified
1. `src/bugalizer/config.py` — Added queue/pipeline settings (7 new fields)
2. `src/bugalizer/models.py` — Phase gating renamed + updated, added AnalysisResponse, UsageSummary
3. `src/bugalizer/db.py` — Added asyncio.Lock, try_claim_report(), analyses CRUD, token_usage CRUD, triage_eligible_reports(), submitted_reports(), reset_triage_retries(), report_update_fields()
4. `src/bugalizer/main.py` — Mounts usage router, starts/stops queue worker in lifespan
5. `src/bugalizer/api/reports.py` — Updated to use enforce_phase_gating parameter
6. `src/bugalizer/api/queue.py` — Added POST /queue/{id}/retry endpoint
7. `pyproject.toml` — Moved litellm from optional to core dependencies
8. `tests/test_api.py` — Updated 6 phase-gating tests for Phase 2 (2 now pass, 4 still blocked)

---

## Round 1

### Lead

**Action:** SUBMIT_FOR_REVIEW

Phase 2 (Local LLM Pipeline) implementation is complete. All 62 tests pass (30 existing + 32 new).

**What was delivered (matches approved plan):**

**1. LLM Client (`llm/client.py`):**
- Async litellm wrapper: `complete(model, messages, ...)` → `LLMResponse`
- Returns structured response with content, prompt/completion tokens, model, provider
- Configurable via `BUGALIZER_OLLAMA_HOST`, defaults to `ollama/{default_triage_model}`
- Auto-prefixes `ollama/` to model names

**2. Prompt Templates (`llm/prompts.py`):**
- `TRIAGE_SYSTEM_PROMPT` — Bug triage specialist, JSON-only output
- `TRIAGE_USER_TEMPLATE` — Formats report fields, requests structured JSON
- `format_triage_prompt()` — Builds message list from report dict

**3. Pipeline Stage 1 — Validator (`pipeline/validator.py`):**
- `extract_structured_data()` — Regex extraction of URLs, file paths, stack traces, error messages
- `find_duplicate()` — difflib SequenceMatcher against existing reports, configurable threshold
- `validate_report()` — Returns result with extracted data + duplicate match
- Tests: 10 covering extraction, duplicate matching, self-exclusion, validation flow

**4. Pipeline Stage 2 — Triage (`pipeline/triage.py`):**
- `triage_report()` — Sends report to LLM, parses JSON, updates report fields + analysis record
- Handles markdown code block wrapping in LLM response
- On success: updates severity/feature_area, stays triaged or → clarification_needed
- On failure: saves failed analysis, rolls back to triaged
- Logs token usage to token_usage table
- Tests: 3 (success, needs_clarification, failure — all with mocked LLM)

**5. Pipeline Orchestrator (`pipeline/orchestrator.py`):**
- `process_submitted()` — Claims (submitted→validating), runs Stage 1, transitions
- `process_triaged()` — Claims (triaged→analyzing), runs Stage 2
- Uses `db_write_lock` for all DB writes
- On failure: rolls back claim status
- Tests: 4 (validate+triage, duplicate detection, double-claim prevention, full flow with mocked LLM)

**6. Queue Worker (`queue/worker.py`):**
- Async poll loop with configurable interval and semaphore concurrency
- `start_worker()` / `stop_worker()` — FastAPI lifespan integration
- Polls `submitted_reports()` for Stage 1, `triage_eligible_reports()` for Stage 2
- Disabled in tests via `BUGALIZER_QUEUE_ENABLED=false`

**7. Queue Claim & Idempotency:**
- `try_claim_report()` — Atomic `UPDATE WHERE status=?` + rowcount check
- Prevents double-processing under concurrency
- Tests: 1 explicit double-claim test

**8. Triage Retry Rules:**
- `triage_eligible_reports()` — Checks for: no successful triage, failed past retry delay, under max retries
- `reset_triage_retries()` — Deletes failed analyses for manual retry
- Tests: 5 (never attempted, completed excluded, failed past delay, within delay excluded, max retries excluded)
- Plus 1 reset test

**9. SQLite Concurrency:**
- `db_write_lock = asyncio.Lock()` — All worker DB writes serialized
- WAL mode already active

**10. Usage Endpoints (`api/usage.py`):**
- `GET /api/v1/usage` — Aggregate across all projects
- `GET /api/v1/usage/{project_id}` — Per-project breakdown
- Tests: 3 (empty, with data, per-project)

**11. Retry Endpoint (`api/queue.py`):**
- `POST /api/v1/queue/{id}/retry` — Validates status=triaged, resets failed analyses
- Tests: 3 (not found, wrong status, success)

**12. Phase Gating:**
- `PHASE1_ALLOWED_TARGETS` → `CURRENT_PHASE_TARGETS` (adds `analyzing`, `clarification_needed`)
- `phase1_only` → `enforce_phase_gating` parameter
- Phase 2 tests: `analyzing` and `clarification_needed` now pass; `fix_proposed`, `fix_approved`, `fix_committed`, `verified` still blocked

### Reviewer

**Action:** REQUEST_CHANGES

Implementation is close, but I found blocking issues to resolve before approval:

1. Queue worker concurrency is not actually concurrent.
   - In `src/bugalizer/queue/worker.py:30` and `src/bugalizer/queue/worker.py:35`, each report is awaited inline inside the loop.
   - `asyncio.Semaphore` does not create parallelism by itself here; the code still processes one report at a time.
   - This means `queue_max_concurrent` is effectively ignored.
   - Requested fix: dispatch report processing as tasks (bounded by semaphore) and await the task set per poll cycle.

2. DB write lock is held across the entire LLM network call, serializing the whole worker path.
   - `src/bugalizer/pipeline/orchestrator.py:90` wraps `await triage_report(report)` in `db_write_lock`.
   - `triage_report` performs the remote LLM call at `src/bugalizer/pipeline/triage.py:54`.
   - Result: while one triage call is waiting on the model, all other lock-protected worker operations (claims, status updates, validation writes) are blocked.
   - Requested fix: limit lock scope to DB mutation sections only; do not hold it during external I/O.

3. Planned SQLite `OperationalError` retry/backoff was not implemented.
   - The approved Phase 2 plan explicitly called for bounded retry/backoff on `sqlite3.OperationalError: database is locked`.
   - I could not find that behavior in worker/orchestrator/db write paths.
   - Requested fix: add retry/backoff around queue-worker DB write operations and add tests covering retry-on-locked behavior.

## Round 2

### Lead

**Action:** SUBMIT_FOR_REVIEW

All 3 blocking issues fixed. Tests expanded from 62 to 66, all passing.

**1. Queue worker concurrency — now truly concurrent:**
- `worker.py` rewritten: reports dispatched as `asyncio.create_task()` calls bounded by semaphore
- Tasks collected into a list, then `asyncio.gather(*tasks, return_exceptions=True)` awaits the full batch per poll cycle
- `_process_with_semaphore()` helper wraps each coroutine in `async with semaphore`
- `queue_max_concurrent` now actually limits parallel processing

**2. DB write lock scope narrowed — NOT held during LLM calls:**
- `orchestrator.py`: `process_triaged()` no longer wraps `triage_report()` in `db_write_lock`. Lock only held for the initial claim.
- `triage.py`: Restructured to separate concerns:
  1. `analysis_create()` under lock (creates pending record)
  2. `await complete()` — LLM network call **outside** any lock
  3. All result DB writes (`analysis_update`, `report_update_fields`, `token_usage_create`, `report_update_status`) under a single lock acquisition
  4. Error path: failure writes also under lock
- `orchestrator.py`: `process_submitted()` batches DB writes (analysis_create + status update) into a single lock acquisition

**3. SQLite OperationalError retry/backoff implemented:**
- Added `retry_on_locked` decorator in `db.py`
- Exponential backoff: 0.1s, 0.2s, 0.4s (3 retries)
- Only retries `sqlite3.OperationalError` containing "database is locked"; other errors propagate immediately
- Applied to 6 worker-facing DB write functions: `try_claim_report`, `report_update_status`, `report_update_fields`, `analysis_create`, `analysis_update`, `token_usage_create`
- **4 new tests** in `test_queue.py`:
  - `test_retry_on_locked_succeeds_on_first_try` — no error, called once
  - `test_retry_on_locked_retries_then_succeeds` — fails first, succeeds on retry
  - `test_retry_on_locked_exhausts_retries` — always fails, raises after all retries
  - `test_retry_on_locked_ignores_other_errors` — non-locked error not retried

### Reviewer

**Action:** APPROVE

Approved. All previously requested blocking fixes are now in place:
- Queue worker now dispatches work as tasks and enforces true bounded concurrency via semaphore + gather
- `db_write_lock` scope is narrowed so LLM network I/O is outside the lock
- SQLite lock retry/backoff is implemented via `retry_on_locked` and covered by dedicated tests

Validation run:
- `pytest -q` -> 66 passed, 1 warning (pytest cache permission warning only)

---

<!-- CYCLE_STATUS -->
READY_FOR: lead
ROUND: 2
STATE: approved
