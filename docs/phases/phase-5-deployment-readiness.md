# Phase 5 — Deployment Readiness & Queue Dashboard

**Status:** PLAN REVIEW — submitted to tagteam (lead: claude, reviewer: codex)
**Drafted:** 2026-07-02 · **Updated:** 2026-07-03 (Phase 4 impl approved; 5.0 housekeeping largely done)
**Goal:** Make Bugalizer safe to host permanently on the LAN (Windows / RTX 4070 Super) so other
apps can submit bugs to it, the user can watch bugs stack up in a queue dashboard, and each bug
can be analyzed on (a) the local LLM or (b) cloud AI.

## Why this phase

A full re-evaluation (2026-07-02) found Phases 1–4 implemented and all tests passing (142 as of
2026-07-03, after the Phase 4 impl review), but four blockers for always-on hosting:

1. **Unbounded paid retries.** Stage 3 (localization) and Stage 4 (fix proposal) failures reset the
   report to `triaged` with no failure record, no retry cap, and no backoff. A persistently failing
   report is retried every `queue_poll_seconds` (5s) forever — for Stage 4 that is an unbounded
   loop of paid Anthropic calls. (Triage already does this right: `max_triage_retries=3` +
   `retry_delay_seconds` enforced in `db.py::triage_eligible_reports`.)
2. **No dashboard.** Phase 5 (original plan) never started. Queue visibility today is
   `GET /api/v1/queue` counts and Swagger at `/docs`.
3. **No local-vs-cloud choice per bug.** The pipeline is hardwired: Ollama for triage/localize,
   `fix_provider`/`default_fix_model` for fixes. `QA_LLM_*` env fallbacks swap providers globally
   only. The per-project `llm_provider`/`llm_model` fields on the Project model are stored but
   never read by the pipeline.
4. **Insecure defaults, no deploy tooling.** Auth off when `BUGALIZER_API_KEYS` is empty (the
   default), CORS `allow_origins=["*"]`, `/health` is static (doesn't check DB or Ollama), and
   there is no Dockerfile / service definition / deploy doc.

Housekeeping status (2026-07-03): the uv migration is committed, the Phase 4 tagteam review has
been queued **and approved** (round 2, commit `498fef1`), `docs/phases/architecture.md` now exists,
the README/decision-log/`.env.example` de-staling landed (commit `3f78ce3`), and Stage 4's
unbounded-retry concern (blocker #1) was partially de-risked — Stage 4 no longer runs on stale
localization, though the retry *cap/backoff* itself is still open and remains 5.1's job. Still
outstanding: `docs/llm-tiering.md` is referenced by `config.py` / `llm/client.py` but does not
exist (fold into `architecture.md` or repoint); Fernet key-encryption remains an unimplemented stub
(`settings.secret_key`, `projects.api_key_encrypted` column, no `cryptography` dependency) — 5.2
decides its fate.

## Scope

**In:** worker reliability, security hardening, per-report analysis-tier selection, minimal queue
dashboard, deployment packaging for the Windows LAN box, doc/debt cleanup.
**Out:** Phase 6 integrations (webhooks, sonicgrid/qaagent hookups), multi-node scaling (SQLite +
single process is fine for one LAN host), auth beyond static API keys.

---

## 5.0 Housekeeping (prerequisite, no behavior change)

- [x] Commit the uv migration (`pyproject.toml`, `uv.lock`, `.python-version`, deleted
      `requirements.txt`, CLAUDE.md/README wording). — done, commit `05b74b0`.
- [x] Fix stale README: Phase 4 is implemented and wired into the worker (`queue/worker.py:71-77`),
      not "planned". — done, commit `3f78ce3`.
- [~] Resolve dangling doc references: `docs/phases/architecture.md` now exists (commit `62ba64d`);
      `docs/llm-tiering.md` is still referenced by `config.py` / `llm/client.py` and must be either
      written or folded into `architecture.md` and repointed. Recommendation: fold into
      `architecture.md`.
- [x] Queue the overdue Phase 4 review as the first tagteam handoff. — done; approved round 2,
      commit `498fef1`.
- [~] Fill in `docs/decision_log.md` with this phase's decisions, starting with 5.2's secrets
      decision. — template seeded (commit `62ba64d`); 5.2's Fernet decision still to be recorded.

## 5.1 Worker reliability (blocker — do before any always-on deployment)

Mirror the triage retry pattern for Stages 3 and 4:

- [ ] **Stage 3 (localization):** on failure, write a failure record (parallel to the failed
      analysis row triage writes) instead of silently resetting to `triaged`. Enforce
      `max_localize_retries` (new setting, default 3) and `retry_delay_seconds` in
      `db.py::localization_eligible_reports`.
- [ ] **Stage 4 (fix proposal):** same, with `max_fix_retries` (new setting, default **2** — each
      retry is a paid cloud call) enforced in `db.py::reports_eligible_for_fix`. Distinguish
      transient errors (timeout, 429/5xx → retry) from permanent ones (invalid diff output,
      auth failure → no retry) where litellm exposes the difference.
- [ ] Reports that exhaust retries stay in `triaged` but are excluded from dispatch by the
      eligibility queries themselves — retry exhaustion is *derived from failed Stage 3/4 analysis
      rows* (mirroring how `triage_eligible_reports` counts failed triage rows), not stored as a
      separate status. `failed_stage` / `last_error` surfaced in `GET /reports/{id}` and the queue
      overview are computed from the latest failed analysis row for the stage. `POST
      /api/v1/queue/{id}/retry` re-enables a report by deleting its failed Stage 3/4 analysis rows
      (today it only clears triage), which drops the derived count back below the cap. (Reviewer-
      resolved: keep exhausted reports in `triaged`, do not overload `deferred`.)
- [ ] **Real health check:** `/health` should report DB reachability and Ollama reachability
      (cheap `GET {ollama_host}/api/tags` with short timeout), plus worker-alive status. Keep a
      liveness-only variant for the process supervisor.
- [ ] Optional hardening: skip dispatching Ollama stages for one poll cycle after an
      Ollama-connectivity failure (simple cooldown, not a full circuit breaker).

**Acceptance:** a report whose localization or fix stage always fails ends in a visible failed
state after N attempts, with the error recorded; no unbounded Anthropic spend is possible; tests
cover retry-cap exhaustion and the retry endpoint for both stages.

## 5.2 Security hardening (blocker)

- [ ] **Auth on by default for LAN:** keep the empty-keys-disables-auth behavior for tests/dev,
      but log a prominent startup warning, and document generating a key in the deploy guide.
      The deployed instance MUST set `BUGALIZER_API_KEYS`.
- [ ] **CORS:** replace `allow_origins=["*"]` (`main.py:42`) with a `BUGALIZER_CORS_ORIGINS`
      setting, defaulting to same-origin only (the dashboard in 5.4 is served by this app, so it
      needs no CORS at all; other LAN apps talk server-to-server with API keys, not from browsers).
- [ ] **Decide the Fernet stub:** recommendation — **remove it** (drop `settings.secret_key` and
      the unused `projects.api_key_encrypted` column reference) and record the decision:
      single-user LAN service, secrets come from env (`BUGALIZER_ANTHROPIC_API_KEY`), at-rest
      encryption deferred until multi-tenant use. The alternative (actually implementing Fernet +
      `cryptography` dep) is more code for no current threat-model gain.
- [ ] Run `/security-check` skill before handoff.

**Acceptance:** deployed config requires an API key; CORS is closed by default; no half-implemented
crypto remains in the codebase; decision logged.

## 5.3 Per-report analysis tier: local LLM vs cloud AI

**Analysis mode.**

- [ ] Add `analysis_mode` to bug reports: `auto` (**default**, preserves today's behavior: local
      triage + localize, cloud fix when eligible), `local_only` (never call the cloud; stop after
      localization), `hold` (validate + dedupe only; wait for a human to pick a tier).
- [ ] Eligibility queries in `db.py` respect `analysis_mode`: `hold` reports never auto-dispatch to
      any stage; `local_only` reports are never eligible for Stage 4 (`reports_eligible_for_fix`
      excludes them). `auto` is unchanged.
- [ ] Add `POST /api/v1/reports/{id}/analyze` with body `{"tier": "local" | "cloud"}` (the
      dashboard's two buttons):
      - `local` → run/re-run triage + localization on the local provider.
      - `cloud` → run the Stage 4 fix proposal. Requires a completed, SHA-fresh localization (409
        otherwise, matching the Phase 4 eligibility rules in `reports_eligible_for_fix`). Manual
        `analyze cloud` is an explicit user action, so it overrides `local_only`/`hold` for that
        one report — the mode gates *automatic* dispatch, not an explicit request.

**Model/provider precedence and scope (reviewer-required amendment).**

The Project model has a *single* `llm_provider`/`llm_model` pair defaulting to
`ollama` / `qwen2.5-coder:7b` (`models.py:190-191`). To avoid regressing Phase 4 — where Stage 4
must go to the cloud fix provider (`fix_provider` / `default_fix_model`, default
`anthropic` / `claude-sonnet-4-6`) — the two are kept in **separate namespaces**:

- [ ] **Existing `llm_provider` / `llm_model` scope local stages ONLY** (triage + localization).
      They are never consulted by Stage 4. Wiring them in means: triage/localize resolve their
      model as `report/project override → global `default_triage_model` / `default_localize_model``.
      A project left at the `ollama` default therefore keeps exactly today's local behavior.
- [ ] **Stage 4 gets its own optional per-project override fields**, added in this slice:
      `fix_llm_provider` / `fix_llm_model` (nullable; `NULL` = use global `fix_provider` /
      `default_fix_model`). Stage 4 resolves as `report fix override → project fix override →
      global fix settings`. The default project (with these `NULL`) keeps the Phase 4 cloud fix
      path untouched.
- [ ] **Precedence, stated once:** for every stage the order is
      `per-report override → per-project override → global setting`. Local stages read the
      `llm_*` fields; Stage 4 reads the `fix_llm_*` fields. There is no path by which a project's
      `llm_provider=ollama` reaches Stage 4.
- [ ] `analysis_mode=local_only` stops before Stage 4 regardless of any `fix_llm_*` value (mode
      gate wins over provider config). `POST .../analyze {"tier":"cloud"}` uses the resolved Stage 4
      fix provider/model (per the precedence above), not the local `llm_*` fields.

**Acceptance:** a bug can sit in the queue untouched (`hold`), be analyzed locally on demand, and be
escalated to cloud AI on demand; `auto` preserves today's behavior; **an existing project with the
default `llm_provider=ollama` still runs Stage 4 through the cloud fix provider, never Ollama**;
tests cover mode gating AND provider resolution for each stage (explicitly: default-project Stage 4
resolves to `anthropic`/`claude-sonnet-4-6`, and a project `fix_llm_*` override is honored).

## 5.4 Minimal queue dashboard ("watch bugs stack up")

Deliberately small — no build tooling, no framework. One static HTML page + vanilla JS (or htmx),
served by FastAPI at `/`, polling JSON endpoints every few seconds.

- [ ] `GET /api/v1/reports` gains the list/filter shape the dashboard needs (status filter,
      sort by created_at, include `failed_stage`/`last_error`, pagination) — extend, don't
      duplicate, the existing endpoint in `api/reports.py`.
- [ ] Dashboard page:
      - Queue columns by status (submitted → triaged → analyzing → fix_proposed → terminal),
        with counts from `GET /api/v1/queue`.
      - Per-report row: title, severity, project, age, retry/error badge.
      - Detail view: triage result, localization candidates, fix proposal diff (from
        `GET /reports/{id}/fix_proposals`).
      - Actions: **Analyze (local)**, **Analyze (cloud)** (→ 5.3 endpoint), **Retry** (→ queue
        retry endpoint).
      - API-key entry stored in localStorage, sent as `X-API-Key`.
- [ ] Token-usage summary panel (endpoints already exist in `api/usage.py`).

**Acceptance:** open `http://<lan-host>:8090/`, watch submitted bugs appear and move through
states without refreshing the API docs; both analyze buttons work end-to-end.

## 5.5 Deployment packaging (Windows LAN box, RTX 4070 Super)

Target: Ollama runs **natively on Windows** (GPU access), Bugalizer runs as a supervised service
pointing at it via `BUGALIZER_OLLAMA_HOST`.

- [ ] `Dockerfile` + `docker-compose.yml` (app only; volume-mount `bugalizer.db`, `repos/`,
      `cache/`; `restart: unless-stopped`; healthcheck hits the liveness endpoint;
      `BUGALIZER_OLLAMA_HOST=http://host.docker.internal:11434`).
- [ ] Fallback for no-Docker: NSSM or Task Scheduler service recipe running
      `uv run uvicorn bugalizer.main:app`.
- [ ] `.env.example` documenting every `BUGALIZER_*` var with sane LAN defaults, plus the
      `QA_LLM_*` fallback layer.
- [ ] `docs/deploy-windows.md`: install steps, Ollama model pulls (`qwen2.5-coder:7b`), key
      generation, backup note (SQLite file copy while stopped, or `sqlite3 .backup`), how other
      LAN apps submit bugs (curl example with `X-API-Key`).
- [ ] Smoke-test doc: submit a bug from another machine on the LAN, watch it on the dashboard,
      run local analysis, escalate one to cloud.

**Acceptance:** service survives a reboot, is reachable from other LAN machines, and the smoke
test passes end-to-end against real Ollama.

---

## Ordering & handoff plan

Each numbered slice is one tagteam handoff cycle (claude implements → codex reviews):

1. **Cycle 0 (DONE):** overdue Phase 4 review (approved, `498fef1`) + 5.0 housekeeping commits
   (`05b74b0`, `3f78ce3`, `62ba64d`). Residual: `docs/llm-tiering.md` repoint + decision-log fill,
   both folded into Cycle 1's 5.2 work.
2. **Cycle 1 (NEXT):** 5.1 worker reliability + 5.2 security (the two blockers; ship together —
   they're both small, code-adjacent changes).
3. **Cycle 2:** 5.3 analysis-tier selection (API + pipeline gating; dashboard-independent).
4. **Cycle 3:** 5.4 dashboard.
5. **Cycle 4:** 5.5 deployment packaging + real-hardware smoke test on the Windows box.

Definition of done for the phase: all acceptance criteria above; full test suite green
(142 existing + new coverage for retries, analysis modes, and the reports list endpoint);
service running on the LAN box past a reboot; smoke test recorded in `docs/decision_log.md`.

## Resolved decisions (round 1, reviewer-confirmed)

1. **`analysis_mode` default = `auto`.** Bugs flow through the local stages immediately; `hold` and
   `local_only` are opt-in per report.
2. **Retry-exhausted reports stay `triaged`, not `deferred`.** Exhaustion is *derived from failed
   Stage 3/4 analysis rows* by the eligibility queries (mirroring triage), so an exhausted report is
   simply not re-dispatched; `failed_stage`/`last_error` are computed from the latest failed row.
   `POST /queue/{id}/retry` clears those failed rows to re-enable dispatch. (See §5.1.)
3. **Fernet stub: remove it.** Drop `settings.secret_key` and the unused
   `projects.api_key_encrypted` reference; secrets come from env only. Decision recorded in
   `docs/decision_log.md` as part of §5.2.
4. **Dashboard tech: plain fetch-polling** (no htmx/SSE) — matches the worker's own 5s cadence and
   keeps zero front-end dependencies.
5. **Stage 4 provider isolation (from round-1 §5.3 changes):** per-project `llm_provider`/`llm_model`
   scope local stages only; Stage 4 uses `fix_llm_*` overrides falling back to global
   `fix_provider`/`default_fix_model`, so a default `ollama` project never routes cloud fixes to
   Ollama. (See §5.3.)
