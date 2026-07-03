# Bugalizer Architecture

**Status:** reflects the codebase as of 2026-07-02 (Phases 1–4 implemented, 139 tests).
Phase-by-phase plans live alongside this file in `docs/phases/`; the current active plan is
[phase-5-deployment-readiness.md](phase-5-deployment-readiness.md).

## Overview

Bugalizer is a standalone Python/FastAPI service that accepts structured bug reports over REST,
stores them in SQLite, and progressively enriches them through a tiered AI pipeline. The design
principle is **cheapest-capable tier first**: free deterministic processing, then a local LLM
(Ollama), and only for the final fix-proposal stage a paid cloud LLM (Anthropic). This mirrors
the "Agentless" research finding that a fixed localize→repair pipeline beats open-ended agent
loops on cost without losing accuracy.

```
Client --> FastAPI --> SQLite (WAL) <-- Queue worker (asyncio poll loop)
              |                              |
           REST API                      Pipeline stages
           X-API-Key auth                  1 Validate   (no LLM)
           OpenAPI /docs                   2 Triage     (Ollama)
                                           3 Localize   (Ollama, two-pass)
                                           4 Fix        (Anthropic via litellm)
```

## Components

| Module | Responsibility |
|--------|----------------|
| `main.py` | FastAPI app, lifespan (starts/stops queue worker), CORS, `/health`, serves the dashboard at `/` |
| `config.py` | Pydantic BaseSettings, `BUGALIZER_*` env prefix, `QA_LLM_*` fallback layer |
| `auth.py` | `X-API-Key` header auth; disabled when `BUGALIZER_API_KEYS` is empty |
| `models.py` | Pydantic models, 13-state workflow, `CURRENT_PHASE_TARGETS` phase gating |
| `db.py` | SQLite schema + CRUD, `retry_on_locked`, `db_write_lock`, ad-hoc `_migrate()` |
| `api/` | Routers: reports, projects, queue, usage |
| `llm/client.py` | litellm wrapper: `ollama` / `anthropic` providers + generic passthrough |
| `pipeline/` | Stage implementations + `orchestrator.py` coordinator with atomic claims |
| `git_ops/repo.py` | Clone/pull/SHA/file listing via subprocess |
| `queue/worker.py` | Background asyncio poll loop dispatching all four stages |
| `static/dashboard.html` | Queue dashboard (Phase 5 §5.4): one self-contained page, vanilla JS, 5s fetch-polling; API key in localStorage sent as `X-API-Key`; analyze/retry actions |

## Pipeline stages

1. **Validate** (`pipeline/validator.py`, no LLM): extracts structured data (URLs, paths, stack
   traces), fuzzy-matches title+description against existing reports for duplicate detection
   (`duplicate_threshold`, default 0.8). Duplicates terminate with
   `resolution_reason=duplicate_of:<id>`.
2. **Triage** (`pipeline/triage.py`, Ollama): classification — severity, feature area,
   clarification flag. Failed attempts are recorded; eligibility (`triage_eligible_reports`)
   enforces `max_triage_retries` (3) and `retry_delay_seconds` (60).
3. **Localize** (`pipeline/localizer.py`, Ollama, two-pass): pass 1 sends a tree-sitter repo map
   (`pipeline/repo_map.py`, SHA-cached, multi-language) to shortlist candidate files; pass 2
   reads actual file contents to pinpoint functions/line ranges. LLM-provided paths go through
   `_validate_candidate_path()` (path-traversal protection). Freshness is SHA-based:
   `project.head_sha` vs `localization.repo_sha`.
4. **Fix** (`pipeline/fix_proposer.py`, Anthropic via litellm): bundles localized file contents
   (size-capped), calls the fix model with a prompt-cached system prompt, persists a unified-diff
   proposal (root cause, explanation, confidence, files_changed) to `fix_proposals`. Transitions
   `TRIAGED → FIX_PROPOSING → FIX_PROPOSED`, resetting to `TRIAGED` on failure.

All four stages are dispatched automatically by `queue/worker.py` each poll cycle
(`queue_poll_seconds`, default 5s), bounded by `asyncio.Semaphore(queue_max_concurrent)`.
Double-processing is prevented by atomic claims (`try_claim_report()`, compare-and-set on
status). Blocking git/AST/file work runs under `asyncio.to_thread()`.

> Known Phase 5 work item: Stages 3–4 lack the retry cap/backoff that Stage 2 has — see
> [phase-5-deployment-readiness.md](phase-5-deployment-readiness.md) §5.1.

## Workflow states and phase gating

13 report states (`models.py`): `submitted → validating → triaged → analyzing /
clarification_needed / fix_proposing → fix_proposed → fix_approved → fix_committed → verified`,
plus terminals `closed`, `rejected`, `duplicate`, and parking state `deferred`.

`validate_transition()` only allows targets listed in `CURRENT_PHASE_TARGETS` (`models.py`).
`fix_approved`, `fix_committed`, and `verified` are still gated — they are the human-approval
states owned by the Phase 5 dashboard. `analyzing` and `fix_proposing` are transient claim
states; reports return to `triaged` after localization (freshness is tracked by SHA, not state).

## LLM tiering

Model/provider resolution, cheapest tier first:

- **Tier 0 — free:** Stage 1 runs no LLM.
- **Tier 1 — local (Ollama):** Stages 2–3 use `default_triage_model` / `default_localize_model`
  (default `qwen2.5-coder:7b`) against `ollama_host`. In `llm/client.py`, provider `"ollama"`
  normalizes bare model names to `ollama/<model>`.
- **Tier 2 — cloud:** Stage 4 uses `fix_provider` + `default_fix_model` (default
  `anthropic` / `claude-sonnet-4-6`), key from `BUGALIZER_ANTHROPIC_API_KEY`, prompt caching on
  by default (`fix_enable_prompt_caching`).
- **Generic passthrough:** any other provider string routes the litellm model string verbatim;
  credentials come from provider-native env vars (litellm reads `OPENAI_API_KEY` etc. itself).

**`QA_LLM_*` fallback layer** (`config.py::_apply_generic_llm_fallbacks`): for hosts that
configure LLM access generically across apps (e.g. alongside qaagent), `QA_LLM_MODEL` and
`QA_LLM_API_BASE` act as defaults that explicit `BUGALIZER_*` settings always override.
The fix model and provider fall back **atomically** (a pinned `fix_provider` blocks the generic
model, so the pair can never mismatch). Triage/localize are local-only: they consume
`QA_LLM_MODEL` only when it is an `ollama/...` string; a cloud model string leaves them alone.
`QA_LLM_API_BASE` overrides `ollama_host` unless explicitly set.

**Per-project overrides (Phase 5 §5.3)** — resolved in `llm/client.py::resolve_local_llm` /
`resolve_fix_llm`, precedence `per-project override → global setting`, in two separate
namespaces:

- **Local stages (2–3):** project `llm_provider` / `llm_model` (defaults `ollama` /
  `qwen2.5-coder:7b`) → global `default_triage_model` / `default_localize_model`. Never
  consulted by Stage 4.
- **Stage 4:** project `fix_llm_provider` / `fix_llm_model` (nullable; `NULL` = use global
  `fix_provider` / `default_fix_model`, per-field). There is no path by which a project's
  `llm_provider=ollama` reaches Stage 4.

**Per-report analysis tier (Phase 5 §5.3)** — `bug_reports.analysis_mode` gates *automatic*
worker dispatch: `auto` (default, full pipeline), `local_only` (stops before Stage 4), `hold`
(validate + dedupe only). `POST /reports/{id}/analyze {"tier": "local"|"cloud"}` dispatches
manually and overrides the mode for that one run; the cloud tier 409s unless a SHA-fresh
localization exists (same rule as `reports_eligible_for_fix`).

## Storage and reliability patterns

- **SQLite, WAL mode**, single file (`db_path`). Concurrent reads; writes serialized by
  `db_write_lock` (asyncio.Lock) with a `retry_on_locked` decorator for contention. Suitable for
  a single-node service; horizontal scaling is explicitly out of scope.
- **Schema migrations:** backward-compatible column additions via `_migrate()` in `db.py` — no
  migration framework.
- **Soft delete** for reports (`status=rejected`, `resolution_reason=deleted`).
- **Token usage tracking** per project/report in `token_usage`, exposed via `api/usage.py`.
- **Two-tier field validation:** hard-required fields → 422; recommended fields → `warnings`
  array on the response.

## Security model

- Static API keys (`X-API-Key`), comma-separated in `BUGALIZER_API_KEYS`; empty ⇒ auth disabled
  (dev/test convenience — a deployed instance must set keys; hardening is Phase 5 §5.2).
- Cloud credentials come from environment variables only; nothing is stored encrypted at rest
  (the earlier Fernet plan is being retired — see `docs/decision_log.md`).
- Path-traversal protection on all LLM-supplied file paths.

## Phase history

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Foundation: API, DB, auth, workflow | Complete |
| 2 | Local LLM pipeline: triage, queue worker, dedupe, token tracking | Complete |
| 3 | Codebase analysis: git ops, repo maps, two-pass localization | Complete |
| 4 | Fix proposals: Anthropic unified diffs, prompt caching | Implemented — awaiting review |
| 5 | Deployment readiness + queue dashboard | Planned — see phase-5-deployment-readiness.md |
| 6 | Integrations: webhooks, external trackers | Not started |
