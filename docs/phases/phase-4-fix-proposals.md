# Phase 4 — Fix Proposals (Cloud LLM)

**Status:** IMPLEMENTED — submitted for impl review (this cycle)
**Landed in:** `c0002c3` (Stage 4 fix-proposer), `441dfcb` (QA_LLM_* fallbacks + atomic fix model/provider)

## Summary

Add Stage 4 to the pipeline: for a triaged report with a completed, SHA-fresh localization,
call a cloud LLM (Anthropic via litellm) to generate a unified-diff fix proposal with root
cause, explanation, confidence, and files_changed, persisted to a new `fix_proposals` table
and exposed read-only over the API.

## Scope

**In:**
- `pipeline/fix_proposer.py` — bundle localized file contents (size-capped), build the
  fix-proposal prompt (system prompt marked for Anthropic prompt caching), parse/persist the
  proposal.
- New transient claim state `FIX_PROPOSING`: `TRIAGED → FIX_PROPOSING → FIX_PROPOSED`, with
  reset `FIX_PROPOSING → TRIAGED` on any failure.
- Queue worker Stage 4 dispatch: `reports_eligible_for_fix()` → `process_fix_proposal()` each
  poll cycle, same semaphore bound as Stages 1–3.
- `GET /api/v1/reports/{id}/fix_proposals` (read-only).
- Config: `default_fix_model` (claude-sonnet-4-6), `fix_provider` (anthropic),
  `anthropic_api_key`, `fix_max_bundle_bytes` (4 MiB), `fix_max_file_bytes` (512 KiB),
  `fix_enable_prompt_caching` (true).
- `llm/client.py` gains the `anthropic` provider and a generic litellm passthrough branch;
  `QA_LLM_MODEL` / `QA_LLM_API_BASE` env fallbacks with atomic fix model+provider fallback
  (see `docs/phases/architecture.md`, "LLM tiering").
- Token usage recorded per call, as with Stages 2–3.

**Out (deferred):**
- Applying/validating the diff, opening PRs (`fix_approved` / `fix_committed` / `verified`
  remain phase-gated).
- Per-report or per-project provider selection (Phase 5 §5.3).
- Retry cap/backoff for fix failures (known gap — Phase 5 §5.1; see review notes below).

## Technical Approach

- Eligibility (`db.py::reports_eligible_for_fix`): status `triaged`, completed localization
  whose `repo_sha` matches `project.head_sha`, and no existing fix proposal for that SHA.
- Atomic claim via `try_claim_report()` (compare-and-set) prevents double-processing.
- File bundle honors `_validate_candidate_path()` path-safety on LLM-provided localization
  paths; per-file and total byte caps bound the prompt size.
- System prompt carries `cache_control` content parts when `fix_enable_prompt_caching` is on,
  so repeated Stage 4 calls reuse the cached prefix.
- All LLM calls mocked in tests; schema migration adds the `fix_proposals` table via the
  backward-compatible `_migrate()` pattern.

## Files

- `src/bugalizer/pipeline/fix_proposer.py` (new)
- `src/bugalizer/pipeline/orchestrator.py` (`process_fix_proposal`)
- `src/bugalizer/queue/worker.py` (Stage 4 dispatch)
- `src/bugalizer/db.py` (`fix_proposals` table, `reports_eligible_for_fix`)
- `src/bugalizer/models.py` (`FIX_PROPOSING` state, phase-gating update)
- `src/bugalizer/api/reports.py` (`GET /reports/{id}/fix_proposals`)
- `src/bugalizer/config.py`, `src/bugalizer/llm/client.py` (provider + fallbacks)
- `tests/` — fix_proposer, API, and queue-eligibility coverage (139 total passing)

## Success Criteria

- [x] Eligible report automatically receives a persisted unified-diff fix proposal
- [x] Failure at any point resets `FIX_PROPOSING → TRIAGED` without corrupting state
- [x] Proposals retrievable via `GET /api/v1/reports/{id}/fix_proposals`
- [x] No cloud call unless `BUGALIZER_ANTHROPIC_API_KEY` (or QA_LLM_* equivalent) is set —
      clear error otherwise
- [x] Full suite green: 139 passed

## Notes for reviewer

Known accepted gap, already scheduled: fix-stage failures have **no retry cap or backoff**
(failed reports re-match eligibility every poll cycle — unbounded paid calls on persistent
failure). This is Phase 5 §5.1's first work item (`docs/phases/phase-5-deployment-readiness.md`);
flagging so the review can focus on correctness of what's here, or veto the deferral.
