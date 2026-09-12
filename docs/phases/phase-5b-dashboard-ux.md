# Phase 5b — Dashboard UX & Tier Clarity

**Status:** PLAN REVIEW — submitted to tagteam (lead: claude, reviewer: codex)
**Drafted:** 2026-07-02
**Goal:** Turn the minimal Phase-5 queue dashboard into a genuinely good operator UI. The design
spine: at every point where analysis happens or is displayed, it must be **unmistakable which
tier did the work** — local LLM (free, on the GPU box) vs cloud AI (paid, per-call API cost) —
and **what has already been done**, so a re-scan is always a deliberate choice, never an
accident. Secondary goals: make results readable (triage, diffs, run history), make the box
operable at a glance (health, filters), and eliminate the remaining curl-only workflows
(project management, bug submission). Fake/demo bug reports are the working material — real
repo integration and fix-acceptance flows stay out of scope.

## Why this phase

Phase 5 closed with the hosting milestone: Bugalizer is live on the Windows GPU box at
`https://bugalizer.lan/`, the real-Ollama pipeline verified end-to-end. The decision log entry
(2026-07-02) explicitly deferred "Dashboard UI polish + open bugs" to a next phase. Findings
from bringing the deployment live, plus a review of `static/dashboard.html`:

1. **Tier identity is weak.** "Analyze (local)" / "Analyze (cloud)" buttons exist, and the paid
   button has a two-click confirm, but nothing else in the UI carries the local-vs-cloud
   distinction: results don't say which tier produced them, cards don't show what tier already
   ran, and the mode selector (`auto`/`local_only`/`hold`) is unexplained. The sibling Aegis
   project (`~/projects/QA`, `packages/aegis-qa/.../landing/`) solved this with a
   tier-color design language — steel (deterministic) / emerald (local AI) / violet (cloud AI)
   LED chips — that users already associate with these tiers. Bugalizer should adopt it.
2. **"Already scanned" is nearly invisible.** The drawer has two chips (Triaged / Localized ·
   age) added post-deploy, but cards show only a `✓ localized` badge, and neither says *which
   tier* ran nor whether a cloud (paid) analysis already exists. Accidental re-scans are guarded
   only for local re-runs.
3. **Results are hard to read.** Triage renders as a raw `JSON.stringify` dump — burying
   clarification questions, which the Windows deployment showed are the main human action item
   (the 7b model triages conservatively). Fix diffs are monochrome `<pre>`. The
   `GET /reports/{id}/analyses` endpoint returns per-run provider/model/tokens/cost/timing, but
   the drawer only uses it for the two chips.
4. **Queue states blur together.** The "Triaged" column conflates `triaged`,
   `clarification_needed`, and `deferred` — reports needing human input look identical to
   healthy ones. The Terminal column grows forever.
5. **No ops surface.** `/health` reports database/ollama/worker but the header shows only poll
   status. No filters or search (fixed `limit=200`).
6. **curl-only workflows.** Project create/edit (repo URL, `llm_*`, `fix_llm_*`) and bug
   submission have no UI. The Retry button shows even when nothing failed.

## Scope

### 5b.1 Design system & tier identity (the spine)

Adopt the Aegis "Control Room" design language, adapted to Bugalizer:

- **Themes:** Slate (dark) / Mist (light) CSS custom-property palettes, `data-theme` toggle
  persisted in localStorage (Slate default, matching the current dark dashboard).
- **Tier colors, used everywhere analysis appears:** steel = deterministic (Stage 1
  validation), emerald = local LLM (free · on-device), violet = cloud AI ($ · API cost).
  LED-dot chips in the Aegis style. A one-line tier legend in the header or footer.
- **Tier-labeled artifacts:** every rendered result (triage, localization, fix proposal, run
  history rows) carries a tier chip derived from the analysis row's `llm_provider`
  (`ollama` → local/emerald; `anthropic`/other cloud → cloud/violet) with model name and
  relative age, e.g. `● LOCAL · qwen2.5-coder:7b · 12m ago`.
- **Tier-colored actions:** "Analyze (local · free)" styled emerald; "Analyze (cloud · $)"
  styled violet. Existing confirm flows keep their behavior but adopt tier styling.
- **Scan-state chips (cards + drawer):** per-tier "already done" indicators — e.g.
  `✓ local 12m` / `✓ cloud 2h` — so the queue view answers "what already ran, at whose cost"
  without opening the drawer. Cloud re-scan gets the same guarded re-run treatment local
  re-scan already has (inline confirm, poll-safe), with copy that states a cloud re-run costs
  money and shows when the last cloud run happened.

### 5b.2 Reading results

- **Formatted triage:** replace the JSON dump with rendered fields (category, suggested
  severity, duplicate verdict, summary) and a prominent, styled **clarification questions**
  list when present.
- **Colorized diffs:** client-side line coloring for unified diffs (`+` green, `-` red, `@@` /
  file headers dimmed), per-file section breaks. No syntax-highlighting dependency.
- **Needs-attention treatment:** within the Triaged column, visually distinguish
  `clarification_needed` (amber accent + question-mark badge) and `deferred`; column header
  shows a needs-attention sub-count.
- **Run history timeline:** a drawer section listing all analysis rows — stage, tier chip,
  model, duration, tokens, cost, outcome (completed/failed + error excerpt). This is the
  "what was already done" audit trail backing the scan-state chips.

### 5b.3 Ops visibility

- **Health strip:** header LEDs for database / ollama / worker from `/health`, polled on the
  same 5s cycle; red LED + tooltip when degraded (an Ollama outage on the unattended box is
  currently invisible).
- **Filters & search:** client-side filter bar — project select, severity select, title text
  filter; a "needs attention" quick filter. (Server-side query params deferred until Phase 6
  volume demands them; `limit=200` unchanged.)
- **Terminal column:** collapsed by default to a count, expandable; cards capped with a
  "show all" affordance.

### 5b.4 Doing things from the UI

- **Project management:** a Projects view (modal or panel) — list projects; create (name, repo
  URL, default branch); edit local LLM settings (`llm_provider`/`llm_model`, emerald-labeled
  section) and cloud fix settings (`fix_llm_provider`/`fix_llm_model`, violet-labeled section,
  null = global default, with clear-override control). Uses existing project CRUD endpoints;
  PATCH semantics (exclude_unset, explicit null clears fix override) already support this.
- **Bug submission form:** "New report" action — project select, title, description, severity,
  reporter — POSTing to the existing `POST /reports`. Primary use: generating fake/demo
  reports to exercise the UX.
- **Retry visibility:** show the Retry button only when the report has failed analysis rows
  (`failed_stage` is already surfaced per row); keep it in the run-history section otherwise.

### Backend changes (deliberately minimal)

- The `analyses` table already stores `llm_provider`, `llm_model`, tokens, cost, and
  timestamps per run — no schema change anticipated.
- Audit `GET /reports` list items and `GET /reports/{id}/analyses` for any missing fields the
  cards/timeline need (e.g. compact per-tier scan summary on list rows to avoid N+1 drawer
  fetches); add response fields only as needed, backward-compatibly.
- No new state-machine states; `fix_approved`/`fix_committed`/`verified` remain phase-gated.

### File layout & serving (amended per review, round 1)

`src/bugalizer/static/dashboard.html` (511 lines) will roughly triple, so split it into three
package assets — still zero-build vanilla JS:

- `src/bugalizer/static/index.html` (replaces `dashboard.html`, which is removed)
- `src/bugalizer/static/styles.css`
- `src/bugalizer/static/app.js`

Serving changes in `src/bugalizer/main.py` (there is currently **no** static mount — `/` is a
single `FileResponse(_STATIC_DIR / "dashboard.html")`):

- `/` serves `_STATIC_DIR / "index.html"` (same unauthenticated `FileResponse` pattern; the
  page still carries no secrets — API calls send the user-entered `X-API-Key`).
- Mount `StaticFiles(directory=_STATIC_DIR)` at `/static` (unauthenticated, same-origin);
  `index.html` references `/static/styles.css` and `/static/app.js` by absolute path so the
  links resolve regardless of how `/` was reached (incl. behind the Caddy proxy).
- Packaging: the existing `bugalizer = ["static/*"]` package-data glob already covers the new
  flat files; no `pyproject.toml` change expected (verified in the impl by installing the
  built wheel or checking the sdist file list).

Acceptance tests for the split (the current dashboard test only asserts `/` returns HTML
containing `Bugalizer` / `X-API-Key` and would not catch broken links):

- `GET /` → 200, HTML, unauthenticated, and its content references `/static/styles.css` and
  `/static/app.js`.
- Every `href`/`src` under `/static/` present in the served HTML is fetched and returns 200
  with the expected content type (`text/css`, `text/javascript`/`application/javascript`) —
  the link-integrity check that survives future renames.
- Static assets are served from the *installed package* directory (`_STATIC_DIR`), keeping the
  Docker/NSSM deployments working without doc changes.

## Non-goals

- **Auto-fix acceptance** (accept a proposed fix → create a branch with the patch): explicitly
  future work, requires working-repo integration. The UI may show a disabled/`soon` affordance
  at most.
- Real bug-tracker ingestion (Phase 6), websockets/SSE (polling stays), auth/user management,
  server-side filtering, mobile-first layout (readable on a tablet is enough).

## Handoff cycles

- **Cycle 1 — 5b.1 + 5b.2:** design system, tier identity, scan-state chips, formatted
  triage, colorized diffs, needs-attention, run-history timeline. (The core UX; ships alone.)
- **Cycle 2 — 5b.3 + 5b.4:** health strip, filters, terminal collapse, project management,
  submission form, retry visibility.

Each cycle: implement → self-review → `/handoff start phase-5b-dashboard-ux impl`.

## Success criteria

1. From the board alone (no drawer), the user can tell per report: current state, severity,
   which tiers have already run and how long ago, and whether it needs human attention.
2. Triggering a paid cloud analysis always shows tier color, cost framing, and — if a cloud
   run already exists — its age, before the call fires. No cloud call from a single click.
3. Triage clarification questions are readable at a glance; diffs are colorized; every result
   section names the tier + model that produced it.
4. Projects and reports can be created/edited entirely from the UI (no curl in the demo loop).
5. Health degradation (Ollama down, worker stopped) is visible in the header within one poll.
6. Both themes pass a contrast sanity check; theme choice persists.
7. All existing tests pass; new/changed API response fields covered by tests; the split
   static assets pass the link-integrity acceptance tests above (`/` unauthenticated, linked
   CSS/JS resolve with correct content types, assets ship in package data); dashboard
   live-smoked on the Mac dev server with fake reports covering: never-analyzed, local-only,
   local+cloud, clarification_needed, failed-stage, terminal.
