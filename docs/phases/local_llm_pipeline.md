# Phase: Local LLM Pipeline

## Summary

Add automated bug report processing using local LLMs via Ollama. Reports flow through a two-stage pipeline: (1) validation & pre-processing (no LLM, zero cost) and (2) triage & classification (Ollama via litellm). An async background worker picks up submitted reports and drives them through the pipeline automatically. Token usage is tracked per analysis.

## Scope

### In Scope
- Ollama integration via litellm (connect to local LLM at `OLLAMA_HOST`)
- Pipeline Stage 1: Validation & pre-processing (no LLM)
  - Extract structured data: URLs, file paths, error messages, stack traces from description
  - Fuzzy duplicate detection against existing reports (title + description similarity)
  - Auto-transition: `submitted` → `validating` → `triaged` (or `rejected` / `duplicate`)
- Pipeline Stage 2: Triage & classification (Ollama)
  - Severity classification (critical/high/medium/low)
  - Category assignment (UI, API, data, auth, performance, etc.)
  - Feature area identification
  - Generate structured summary
  - Determine if enough info exists or `clarification_needed`
  - Auto-transition: `triaged` → `analyzing` → `triaged` (with enriched data) or `clarification_needed`
- Async queue worker (asyncio background task)
  - Picks up reports in `submitted` status, runs them through pipeline
  - Picks up `triaged` reports eligible for Stage 2 (no successful triage, or last triage failed and past retry delay; max retries not exceeded)
  - Configurable concurrency and polling interval
  - Graceful shutdown on app lifecycle
- Token usage tracking
  - Log prompt/completion tokens per analysis in `analyses` table
  - Aggregate usage in `token_usage` table per project
  - `GET /api/v1/usage` and `GET /api/v1/usage/{project_id}` endpoints
- Manual retry endpoint
  - `POST /api/v1/queue/{id}/retry` — Resets retry count for a report, making it eligible for re-processing
- Unlock Phase 2 status transitions
  - Remove Phase 1 gating for: `validating`, `analyzing`, `clarification_needed`
  - Keep Phase 3/4 states gated: `fix_proposed`, `fix_approved`, `fix_committed`, `verified`

### Out of Scope (Later Phases)
- Git repo cloning / repo maps (Phase 3)
- Codebase localization (Phase 3)
- Cloud LLM / Anthropic fix proposals (Phase 4)
- Dashboard UI (Phase 5)
- Webhooks / integrations (Phase 6)

## Technical Approach

### LLM Client (`src/bugalizer/llm/client.py`)

Thin wrapper around litellm for calling Ollama models:
- `async def complete(model, messages, **kwargs) -> LLMResponse`
- Returns structured response with content, token counts, model info
- Configurable via `BUGALIZER_OLLAMA_HOST` (default: `http://localhost:11434`)
- Model defaults to project's `llm_model` setting (default: `qwen2.5-coder:7b`)
- Timeout and retry handling

### Prompt Templates (`src/bugalizer/llm/prompts.py`)

Structured prompt templates for each pipeline stage:
- `TRIAGE_SYSTEM_PROMPT` — Instructs the LLM to act as a bug triage specialist
- `TRIAGE_USER_TEMPLATE` — Formats the bug report for analysis, requests JSON output:
  ```json
  {
    "severity": "critical|high|medium|low",
    "category": "string",
    "feature_area": "string",
    "summary": "string",
    "needs_clarification": true|false,
    "clarification_questions": ["string"],
    "confidence": 0.0-1.0
  }
  ```
- Templates use minimal tokens (structured format, no verbose instructions)

### Pipeline Validators (`src/bugalizer/pipeline/validator.py`)

Stage 1 — No LLM, pure Python:
- **Structured data extraction**: Regex patterns to pull URLs, file paths (e.g., `src/foo/bar.py:42`), error messages, stack traces from description text
- **Duplicate detection**: Compare new report's title+description against existing reports using simple text similarity (difflib `SequenceMatcher`, threshold configurable via `BUGALIZER_DUPLICATE_THRESHOLD`, default 0.8). If match found, mark as `duplicate` with reference to the matched report ID.
- **Validation result**: Returns extracted data + duplicate match (if any) as JSON stored in `analyses` table with `phase='validation'`
- **Status outcome**:
  - Duplicate found → `duplicate`
  - Validation passed → `triaged`
  - Missing critical info and no description substance → `rejected` (edge case)

### Pipeline Triage (`src/bugalizer/pipeline/triage.py`)

Stage 2 — Ollama via litellm:
- Takes a triaged report, sends to LLM with triage prompt
- Parses JSON response, stores in `analyses` table with `phase='triage'`
- Updates report fields: `severity`, `feature_area` (if LLM provides better values)
- **Status outcome**:
  - `needs_clarification=true` → `clarification_needed`
  - Success → stays `triaged` (enriched with triage data, ready for Phase 3 localization)
  - LLM failure → analysis row saved with `status='failed'`, report stays `triaged` (eligible for retry)
- Logs token usage to `token_usage` table

### Retry Rules for Failed Triage

A triaged report is eligible for Stage 2 processing when **any** of these are true:
- It has **no** `analyses` rows with `phase='triage'` (never attempted)
- Its **most recent** `phase='triage'` analysis has `status='failed'` **and** `completed_at` is older than `BUGALIZER_RETRY_DELAY_SECONDS` (default: 60)

A triaged report is **not** eligible when:
- It has a `phase='triage'` analysis with `status='completed'` (already triaged successfully)
- Its most recent failed triage is within the retry delay window (back-off)

Maximum retries per report: `BUGALIZER_MAX_TRIAGE_RETRIES` (default: 3). After max retries, the report stays `triaged` but is skipped by the worker. A manual `POST /api/v1/queue/{id}/retry` endpoint resets the retry count.

### Pipeline Orchestrator (`src/bugalizer/pipeline/orchestrator.py`)

Coordinates the pipeline stages for a single report:
- `async def process_report(report_id: str) -> None`
- Runs Stage 1 (validation), checks result
- If triaged, runs Stage 2 (triage)
- Handles errors gracefully (logs, returns report to previous state)
- Each stage creates an `analyses` row tracking status, result, tokens, timing

### Queue Worker (`src/bugalizer/queue/worker.py`)

Asyncio background task:
- Polls DB for reports in `submitted` status (for Stage 1) and `triaged` eligible for Stage 2 (per retry rules above)
- Configurable polling interval: `BUGALIZER_QUEUE_POLL_SECONDS` (default: 5)
- Configurable max concurrent: `BUGALIZER_QUEUE_MAX_CONCURRENT` (default: 2)
- Uses `asyncio.Semaphore` for concurrency control
- Integrates with FastAPI lifespan (start on app startup, cancel on shutdown)
- Logs processing activity

### Queue Claim & Idempotency Strategy

To prevent double-processing under concurrency, reports are **atomically claimed** via compare-and-set status transitions before processing begins:

1. **Stage 1 claim**: Worker attempts `submitted → validating` transition via `UPDATE bug_reports SET status='validating' WHERE id=? AND status='submitted'`. If `rows_affected == 0`, another worker already claimed it — skip.
2. **Stage 2 claim**: Worker attempts `triaged → analyzing` transition via the same pattern. If claim fails, skip.
3. **Claim function**: `db.try_claim_report(report_id, expected_status, new_status) -> bool` — single atomic UPDATE + check `cursor.rowcount`. Returns `True` only if this worker won the claim.
4. **On failure**: If pipeline processing fails after claim, the orchestrator rolls status back to the pre-claim state (`validating` → `submitted` for Stage 1 retry, `analyzing` → `triaged` for Stage 2 retry) and records the failed analysis.

This ensures exactly-once processing per pipeline invocation, even with multiple concurrent workers polling the same DB.

### SQLite Concurrency Strategy

The current codebase uses a single shared `sqlite3.Connection` with WAL mode. With concurrent async workers, write contention is possible. Phase 2 approach:

1. **Single-writer serialization**: All DB writes from the queue worker path go through a shared `asyncio.Lock` (`db_write_lock`). Since workers are async tasks in the same event loop, this serializes writes without blocking the event loop.
2. **Retry on OperationalError**: Any `sqlite3.OperationalError: database is locked` in the worker path retries up to 3 times with exponential backoff (0.1s, 0.2s, 0.4s). This handles edge cases where the API handler is writing concurrently.
3. **WAL mode**: Already enabled (`PRAGMA journal_mode=WAL`), which allows concurrent reads during writes. This is sufficient for the expected load (single server, low concurrency).
4. **No connection pool**: We keep the single shared connection for Phase 2. If SQLite contention becomes a bottleneck, Phase 5 (dashboard + WebSocket) is the natural point to consider connection pooling or a move to PostgreSQL.

### Usage Endpoints (`src/bugalizer/api/usage.py`)

- `GET /api/v1/usage` — Aggregate token usage across all projects (total tokens, total cost, breakdown by provider/model)
- `GET /api/v1/usage/{project_id}` — Per-project usage

### Phase Gating Update

Update `PHASE1_ALLOWED_TARGETS` → `PHASE2_ALLOWED_TARGETS` in `models.py`:
- Add to allowed: `analyzing`, `clarification_needed`
- Still blocked: `fix_proposed`, `fix_approved`, `fix_committed`, `verified`
- Rename the constant and `phase1_only` parameter to be phase-generic (e.g., `CURRENT_PHASE_TARGETS`, `enforce_phase_gating`)

### Config Updates (`src/bugalizer/config.py`)

New settings:
- `ollama_host` — already exists, default `http://localhost:11434`
- `queue_poll_seconds: int = 5`
- `queue_max_concurrent: int = 2`
- `duplicate_threshold: float = 0.8`
- `default_triage_model: str = "qwen2.5-coder:7b"`
- `retry_delay_seconds: int = 60` — minimum wait between triage retries
- `max_triage_retries: int = 3` — max failed triage attempts before skipping

## Files

### New Files
| File | Purpose |
|------|---------|
| `src/bugalizer/llm/__init__.py` | Package init |
| `src/bugalizer/llm/client.py` | litellm wrapper for Ollama calls |
| `src/bugalizer/llm/prompts.py` | Prompt templates for triage |
| `src/bugalizer/pipeline/__init__.py` | Package init |
| `src/bugalizer/pipeline/validator.py` | Stage 1: validation & pre-processing |
| `src/bugalizer/pipeline/triage.py` | Stage 2: triage & classification |
| `src/bugalizer/pipeline/orchestrator.py` | Pipeline coordinator |
| `src/bugalizer/queue/__init__.py` | Package init |
| `src/bugalizer/queue/worker.py` | Async background queue worker |
| `src/bugalizer/api/usage.py` | Token usage endpoints |
| `tests/test_pipeline.py` | Pipeline unit tests (validator, triage with mocked LLM) |
| `tests/test_queue.py` | Queue worker tests |
| `tests/test_usage.py` | Usage endpoint tests |

### Modified Files
| File | Changes |
|------|---------|
| `src/bugalizer/config.py` | Add queue and pipeline settings |
| `src/bugalizer/models.py` | Update phase gating (Phase 2 targets), add analysis/usage response models |
| `src/bugalizer/db.py` | Add CRUD for `analyses` and `token_usage` tables, `try_claim_report()` atomic claim, triage eligibility query |
| `src/bugalizer/main.py` | Mount usage router, start/stop queue worker in lifespan |
| `src/bugalizer/api/reports.py` | Update `phase1_only` → phase-generic gating |
| `src/bugalizer/api/queue.py` | Add `POST /api/v1/queue/{id}/retry` endpoint |
| `pyproject.toml` | Add `litellm` to dependencies |
| `tests/test_api.py` | Update phase-gating tests for Phase 2 allowed targets |

## Success Criteria

1. A report submitted via `POST /api/v1/reports` is automatically picked up by the queue worker and moved through `submitted` → `validating` → `triaged`
2. Duplicate reports are detected and auto-marked as `duplicate` with reference to the original
3. Triaged reports are sent to Ollama for triage, receiving severity/category/summary enrichment
4. Reports needing more info are moved to `clarification_needed` with specific questions
5. All LLM calls log token usage; `GET /api/v1/usage` returns accurate totals
6. Queue worker starts/stops cleanly with the app lifecycle
7. Pipeline handles LLM failures gracefully (report stays in queue for retry, respects retry delay and max retries)
8. `POST /api/v1/queue/{id}/retry` resets retry count and makes a report eligible for re-processing
9. All existing Phase 1 tests continue to pass
10. New tests cover: validation pipeline, duplicate detection, triage with mocked LLM, queue worker behavior, usage endpoints, retry endpoint
11. Phase 2 status transitions (`analyzing`, `clarification_needed`) are unlocked; Phase 3/4 states remain gated

## Dependencies

- `litellm>=1.30` (already in `pyproject.toml` optional `[llm]` group — move to core deps)
- Ollama running at `BUGALIZER_OLLAMA_HOST` with a code-capable model pulled (e.g., `qwen2.5-coder:7b`)
- For tests: LLM calls mocked (no Ollama dependency in CI)
