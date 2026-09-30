# Phase 10: sonicgrid-ingest (B1)

## Summary

B1 of the Aegis Bugalizer arc (direction record in the Aegis repo,
`docs/bugalizer-integration-direction-2026-09-12.html`, rulings D1 to D6). A
sonicgrid user with bug-tool permission files a report through sonicgrid's existing
Report Bug dialog; within one poll interval a copy appears in Bugalizer's queue on
BOWIE and goes through the normal pipeline (Stage 1 validation, then free local
triage). Nothing changes for the reporter: sonicgrid stays the record and its admins
still see the report at `/admin/bugs`. Bugalizer is the analysis workbench fed from it.

Sonicgrid runs on Vercel and cannot reach BOWIE, so Bugalizer **pulls**. The pull
surface is sonicgrid S0 (sonicgrid roadmap Phase 46): `GET
/api/bugalizer/bug-reports?cursor=&limit=` behind a static bearer token. S0 is
complete; its hosted acceptance walk passed 2026-09-30 (sonicgrid commit `551342c7`,
branch `greg/docs-s0-roadmap-status`: "B1 may start"). The contract B1 codes against
is `documentation/BUGALIZER-POLL-ENDPOINT.md` in the sonicgrid repo (merged in
`e1fb2b7d`, PR #581).

B1 writes nothing back to sonicgrid (the contract forbids it; the write-back
columns and admin-page status were deferred in S0). Depends on Phase 7 (B0), whose
ingest seam (`projects.ingest_source` / `ingest_config`) B1 reads unchanged.

### Decisions taken for this plan (arbiter, 2026-09-29, "your recommendations")

- **D-A Sequencing.** B1 lands before Phase 9 (private-repo-access). Ingest stores
  reports without a clone; localizing them on BOWIE needs the sonicgrid clone kept
  current, which is Phase 9's job. Until then the BOWIE clone is refreshed by hand,
  as today.
- **D-B Reporter email is not stored.** `reporter` = `reporterName`. The email is
  dropped at the mapping boundary and never persisted or logged.
- **D-C Acceptance on BOWIE** with the real token (see Success Criteria).

**Revision r2** (codex plan r1): the re-walk gets its own resumable cursor and
in-progress state and never delays forward polling (§4); every import + checkpoint
write is one transaction guarded by a per-project ingest generation, so a config
change, clear or delete during an in-flight poll cannot be undone by stale work
(§3a); `ingest_state` cascades on project delete and `project_delete` clears it
explicitly (§3); the public `/health` ingest block is reduced to counts, details stay
behind the API key (§7); the "spends nothing" claim is qualified by
`BUGALIZER_AUTO_FIX_ENABLED=false` (§2).

## Scope

In:

1. A background **ingest poller**, separate from the queue worker, that polls every
   project with `ingest_source` set.
2. **Idempotent import** keyed on the sonicgrid report id.
3. A persisted **checkpoint** following the contract's cursor rules.
4. A periodic **full re-walk** for reconciliation (late inserts, reopened reports).
5. **Mapping** sonicgrid → Bugalizer report fields.
6. **Token secrecy**: the poll token lives in the env var named by
   `ingest_config.credential_env` and nowhere else.
7. **Operator surface**: ingest status + manual run endpoints (API key); aggregate
   ingest counts on the public `/health`.

Out:

- Any write to sonicgrid (status, ids, comments). Forbidden by the contract.
- Syncing sonicgrid `resolved` into Bugalizer. A report resolved in sonicgrid after
  import stays open in Bugalizer until closed there by hand.
- Downloading attachments. URLs are stored, not fetched.
- Dashboard changes (ingest is visible through the API and `/health`; imported
  reports appear on the board like any other).
- Private-repo clone/refresh (Phase 9).
- A second ingest source kind. `ingest_source` stays the single enum value
  `supabase`, the name B0 shipped; the contract document says this config fits it.

## Technical Approach

### 1. Poller task (`src/bugalizer/ingest/poller.py`)

- Own `asyncio.Task`, started and stopped in `main.py` `lifespan` next to the queue
  worker, gated on new setting `BUGALIZER_INGEST_ENABLED` (default **false**, so dev
  and tests are unchanged; BOWIE sets it true).
- Interval `BUGALIZER_INGEST_POLL_SECONDS` (default 120). Not folded into the
  worker's 5 s loop: a remote call has different failure modes and must not stall
  triage.
- Each tick: `projects_with_ingest()` → for each project, `poll_project(project)`.
  One project's failure is recorded on that project and does not stop the others.
- Pagination: request `limit=100`; keep fetching while the page is full, up to a
  per-tick page cap (`BUGALIZER_INGEST_MAX_PAGES`, default 20) so a first walk of a
  large backlog cannot monopolise a tick. The next tick continues from the stored
  checkpoint.
- HTTP via `httpx.AsyncClient` (already used by `main.py` and
  `git_ops/pull_request.py`), timeout 30 s. The request URL is `ingest_config.url`
  exactly (B0 already rejects a query string, fragment or userinfo), with `cursor`
  and `limit` added as query params.
- Response handling:
  - `200`: validate body shape (pydantic model with `extra="ignore"`; a malformed
    body is a recorded error, not a crash); import each report; then apply the
    checkpoint rules.
  - `400` (bad cursor): record the error, **clear the stored cursor** so the next
    tick walks from the beginning. Safe because import is idempotent (contract rule 4).
  - `401` / `503`: record a fixed error (`unauthorized` / `source_not_configured`),
    leave the checkpoint untouched.
  - `502`, other statuses, network errors, timeouts: record a fixed error, leave the
    checkpoint untouched.
  - Errors never include the token, the Authorization header or the response body.
- Env var named by `credential_env` unset or empty: skip the project, record
  `credential_missing`, make no request.
- Backoff: after a failed tick a project is skipped for
  `min(2^failures, 32)` ticks, reset on success. Keeps a rotated-out token from
  hammering sonicgrid every two minutes.

### 2. Idempotent import (`db.py`)

Migration (`_migrate()`, additive):

```sql
ALTER TABLE bug_reports ADD COLUMN ingest_source TEXT;
ALTER TABLE bug_reports ADD COLUMN external_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_bug_reports_external
    ON bug_reports(project_id, external_id) WHERE external_id IS NOT NULL;
```

New `report_import_page(project_id, generation, mapped_reports, checkpoint_update)
-> int | None` inserts each report with `INSERT ... ON CONFLICT DO NOTHING` against
the partial index and applies the checkpoint update, in one generation-fenced
transaction (§3a); it returns the number inserted, or `None` when the fence failed
and nothing was written. Status
`submitted`, `analysis_mode` `auto`: the worker's Stage 1 picks it up exactly like an
API submission (validation, duplicate detection), then free local triage. Ingest
spends nothing **while `BUGALIZER_AUTO_FIX_ENABLED=false`** (the default, and BOWIE's
setting): `analysis_mode=auto` reports stay eligible for a paid Stage 4 fix whenever
that global opt-in is turned on, exactly like API-submitted reports. B1 does not
change that rule; the acceptance doc warns about it.

The contract's "no Bugalizer schema change" refers to the project config (the ingest
seam); de-duplication by id needs a place to keep the id. `report_create` is unchanged.

`BugReportResponse` gains `attachments`, `ingest_source` and `external_id`
(additive, nullable; `attachments` is already stored and deserialised but not
exposed).

### 3. Checkpoint (`ingest_state` table)

```sql
CREATE TABLE IF NOT EXISTS ingest_state (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,          -- projects.ingest_generation this state belongs to
    cursor TEXT,                          -- forward checkpoint
    rewalk_cursor TEXT,                   -- reconciliation position (§4)
    rewalk_started_at TEXT,               -- non-null = a re-walk is in progress
    last_full_walk_at TEXT,               -- advances only when a re-walk completes
    last_poll_at TEXT,
    last_ok_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    imported_total INTEGER NOT NULL DEFAULT 0
);
```

Plus `projects.ingest_generation INTEGER NOT NULL DEFAULT 0` (migration).

Contract rules, applied literally to **both** cursors:

1. The cursor is sent back verbatim; never parsed, built or edited.
2. `next_cursor` is stored only when non-null.
3. An empty page (`[]`, `null`) keeps the cursor that was sent.
4. No cursor = from the beginning.

**Deletion.** `ON DELETE CASCADE` (foreign keys are ON in `_get_conn`), and
`project_delete` also deletes the `ingest_state` row explicitly in the same
transaction as the project row, so deletion never depends on the pragma. A project
whose imported reports are still active returns `has_reports` as today; soft-deleted
imported reports are cleaned up by the existing path.

### 3a. Commit boundary and config changes

A poll's HTTP round trip can take up to 30 s, during which the project's ingest
config can be changed, cleared, or the project deleted. Locks alone cannot cover
that (a PATCH does not and should not wait on a remote call), so every write a poll
makes is fenced by a **generation**:

- Any change to `ingest_source` or `ingest_config` (including clearing them) bumps
  `projects.ingest_generation` and deletes the project's `ingest_state` row **in the
  same transaction** as the config update (`project_update`, `db.py`).
- A poll acquires the per-project `asyncio.Lock` (shared by the background task and
  the manual run endpoint), **then** re-reads the project row and captures
  `(ingest_source, ingest_config, ingest_generation)`. No config read before the lock
  is used.
- After each page, the imports and the checkpoint update are written in **one**
  `BEGIN IMMEDIATE` transaction (`report_import_page`, under `db_write_lock`) that
  first checks `SELECT ingest_generation FROM projects WHERE id = ?` equals the
  captured generation. On mismatch or a missing project the transaction rolls back
  with nothing written, and the poll stops for that project. The state upsert writes
  the captured `generation`, so it can only create or update state for the current
  config.
- Because imports and checkpoint commit together, a crash mid-page leaves both
  untouched: the page replays (idempotent) and is never skipped.
- Error bookkeeping (`last_error`, `consecutive_failures`) goes through the same
  generation check, so a stale failure cannot resurrect a cleared state row either.

### 4. Reconciliation re-walk

The contract says to re-walk "from a cursor some minutes behind your checkpoint (or
from the beginning)". The cursor is opaque and must not be constructed, so B1 takes
the second option: a full walk from no cursor, deduping by id. It picks up both gaps
the contract names: inserts committed after a later checkpoint was taken, and reports
reopened from `resolved`. At sonicgrid's volume (single-digit active reports) this is
a few requests every 6 hours.

The re-walk is a second, independent walk with its own persisted position:

- **Start.** A re-walk becomes due when `rewalk_started_at` is null and
  `last_full_walk_at` is null or older than `BUGALIZER_INGEST_REWALK_HOURS` (default
  6), or when the manual endpoint is called with `full=true`. Starting sets
  `rewalk_started_at = now`, `rewalk_cursor = null`.
- **Each tick: forward first, then re-walk.** The forward poll runs first with its own
  page cap, so new reports are never delayed by a reconciliation in progress. If a
  re-walk is in progress and the forward poll did not fail, the tick then continues
  the re-walk from `rewalk_cursor` for up to `BUGALIZER_INGEST_REWALK_PAGES` (default
  5) pages. Each re-walk page commits its imports and `rewalk_cursor` together (§3a,
  same generation fence). The forward `cursor` is never read or written by the re-walk.
- **Completion.** An empty page (`[]`, `next_cursor: null`) completes it:
  `last_full_walk_at = rewalk_started_at` (the start time, so anything reopened
  during a long walk is covered by the next one), `rewalk_started_at` and
  `rewalk_cursor` cleared. `last_full_walk_at` moves only here.
- **Restart.** Position and in-progress state are in the DB, so after a process
  restart the next tick resumes from `rewalk_cursor`; nothing restarts from the
  beginning unless the cursor was never stored.
- **Errors.** A re-walk page error is recorded like a forward error (fixed
  vocabulary, shared backoff counter); `rewalk_cursor` and `rewalk_started_at` are
  kept and the next eligible tick retries from the same position.
- **400 on the re-walk cursor** clears `rewalk_cursor` only (the re-walk restarts
  from the beginning, still in progress); the forward cursor is untouched. A 400 on
  the forward cursor clears only the forward cursor.
- A config change (§3a) deletes the state row, which cancels any re-walk in progress.

Only `active` reports are returned, so a re-walk never re-imports a resolved one, and
a report already imported is never updated (sonicgrid edits after import are not
synced; out of scope).

### 5. Mapping (`src/bugalizer/ingest/sonicgrid.py`, pure functions)

| Bugalizer | From sonicgrid |
|---|---|
| `external_id` | `id` |
| `ingest_source` | the project's `ingest_source` (`supabase`) |
| `title` | first non-empty line of `description`, whitespace-collapsed, cut at 80 chars on a word boundary with `…`; `"(no description)"` when empty |
| `description` | `description` verbatim, capped at the model's 50 000 chars with a truncation marker; `"(empty report)"` when empty |
| `reporter` | `reporterName` trimmed, capped at 200; `"sonicgrid user"` when empty. `reporterEmail` is **dropped** (D-B) |
| `attachments` | `[{url, fileName, contentType}]` as JSON, only `https` URLs kept |
| `labels` | `["sonicgrid"]` |
| `severity` | `medium` (sonicgrid collects none) |
| `environment` | `"sonicgrid production"` |

A report that fails to map (missing `id`) is skipped and counted in the tick's error
note; it does not block the rest of the page or the checkpoint.

### 6. Token secrecy

- Read with `os.environ.get(credential_env)` at request time; never cached on a
  module global, never stored in the DB, never passed through settings.
- Sent only as `Authorization: Bearer …` to `ingest_config.url`. httpx
  `follow_redirects=False`, so a redirect cannot carry it to another host (a 3xx is
  recorded as `unexpected_status`).
- `last_error` values come from a fixed vocabulary (`unauthorized`,
  `source_not_configured`, `bad_cursor`, `upstream_error`, `network_error`,
  `timeout`, `malformed_response`, `unexpected_status`, `credential_missing`) plus
  the HTTP status code; no exception text is stored. Logs follow the same rule.
- Tests plant a distinctive token and assert it appears in no log record, no DB
  column, no API response (`/health`, ingest status, reports) and no error string,
  matching the Phase 7 and Phase 8 secrecy tests.

### 7. Operator surface

- `GET /api/v1/projects/{id}/ingest` → `{enabled, ingest_source, credential_present,
  cursor_present, last_poll_at, last_ok_at, last_error, consecutive_failures,
  last_full_walk_at, imported_total}`. `credential_present` is presence only. The
  cursor value is not returned (opaque, not useful to an operator).
  404 for a project without `ingest_source`.
- `POST /api/v1/projects/{id}/ingest/run` (`?full=true` for a re-walk) → runs one
  poll for that project now, synchronously, and returns `{imported, pages,
  last_error}`. Works with `BUGALIZER_INGEST_ENABLED=false` (for the acceptance walk
  and for BOWIE debugging). A per-project `asyncio.Lock` serialises it with the
  background task.
- `/health` stays **public** (it is the readiness probe for the LAN Service
  Manager and Caddy; unchanged status code and `status` semantics). It gains only
  aggregate, non-identifying counts: `"ingest": {"enabled": bool, "projects": n,
  "failing": n}`. No project ids, cursors, error codes or timestamps on the public
  endpoint. Ingest errors do **not** change `status` (reports can still be submitted).
- The two new project endpoints use `Depends(require_api_key)` like every other
  project route; tests cover 401 without a key and 200 with one when
  `BUGALIZER_API_KEYS` is set.

## Implementation notes (impl round 1)

Where the implementation settles something the plan left open, or differs from it:

- **First re-walk timing.** A new `ingest_state` row sets `last_full_walk_at` to its
  creation time, so the first reconciliation is due one interval after the first poll
  instead of immediately (the first forward walk already starts from the beginning).
  §4's "`last_full_walk_at` is null" case therefore only arises for rows written before
  this rule, and is treated as not due.
- **Walk end.** A walk (forward or re-walk) ends on `next_cursor: null` **or** a short
  page (fewer than `limit` reports), which is equivalent to the contract's empty
  `[]`/null page one request earlier. The re-walk completes there.
- **Unmappable reports** (no usable `id`) are skipped, counted in the poll outcome and
  logged as a count; they are not a poll error, so they do not trigger backoff.
- **Identical PATCH.** Re-sending the same `ingest_source`/`ingest_config` does not
  bump the generation or reset the checkpoint; only an actual change does.
- **Settings added** beyond §1: `BUGALIZER_INGEST_PAGE_LIMIT` (default 100, clamped
  1..200) and `BUGALIZER_INGEST_TIMEOUT_SECONDS` (30).
- **Credential lookup (impl r1, codex P1).** `resolve_env_credential(name)` in
  `config.py`: the real process environment wins, else the deployment `.env`
  (`BUGALIZER_ENV_FILE`, default `.env`), read fresh at each use with
  `python-dotenv` (now a declared dependency), never cached, never copied into
  `Settings` or `os.environ`. `Settings` switched to `extra: "ignore"` because
  `forbid` refused to start on `SONICGRID_POLL_TOKEN` in `.env` and echoed its
  value; the lost fail-loud for typo'd `BUGALIZER_*` keys is replaced by a startup
  warning naming them (names only).
- **Failure containment (impl r1, codex P2).** Unparseable attachment URLs are
  dropped at the mapping boundary. Any unexpected exception inside one project's
  poll is caught there: the step's transaction has rolled back, the poll records
  the fixed code `internal_error` through the generation fence (so backoff
  applies) and the tick continues with the next project; `run_tick` has a second
  boundary for failures before the poll starts. `CancelledError` still propagates.
- **Backoff** is in memory (lost on restart) and whole-poll: one bookkeeping write per
  poll, so a successful forward page does not reset a failing re-walk's counter
  (codex plan-approval note).

## Files

| File | Change |
|---|---|
| `src/bugalizer/ingest/__init__.py` | new package |
| `src/bugalizer/ingest/sonicgrid.py` | response models + pure mapping |
| `src/bugalizer/ingest/poller.py` | poll_project, tick loop, start/stop, backoff |
| `src/bugalizer/config.py` | `ingest_enabled`, `ingest_poll_seconds`, `ingest_max_pages`, `ingest_rewalk_hours`, `ingest_rewalk_pages` |
| `src/bugalizer/db.py` | migration (2 report columns, partial unique index, `projects.ingest_generation`, `ingest_state` with cascade), `report_import_page` (generation-fenced), fenced state/error writes, `projects_with_ingest`; `project_update` bumps generation + deletes state on ingest change; `project_delete` deletes state in its transaction |
| `src/bugalizer/models.py` | `BugReportResponse` additive fields; ingest status/run response models |
| `src/bugalizer/api/projects.py` | ingest status + run endpoints; clear state on ingest config change |
| `src/bugalizer/main.py` | lifespan start/stop; `/health` ingest block |
| `tests/test_ingest.py` | new (see Success Criteria) |
| `.env.example`, `docs/deploy-windows.md` | `BUGALIZER_INGEST_*`, `SONICGRID_POLL_TOKEN` |
| `docs/sonicgrid-ingest-acceptance.md` | BOWIE acceptance steps |
| `CLAUDE.md`, `docs/roadmap.md`, `docs/decision_log.md` | status, D-A/D-B/D-C |

## Success Criteria

Tests (`tests/test_ingest.py`, sonicgrid faked with `httpx.MockTransport` or a local
ASGI stub implementing the contract's cursor semantics over an in-memory list):

1. **Idempotency:** the same page imported twice → one row per id; a full re-walk over
   already-imported reports inserts nothing.
2. **Cursor rules:** cursor sent verbatim (byte-equal to the last non-null
   `next_cursor`); null `next_cursor` never overwrites; an empty page keeps the sent
   cursor; first poll sends no cursor.
3. **`limit=1`-style multi-page walk** across same-timestamp reports imports each once,
   in order.
4. **Page cap:** a backlog larger than `max_pages × limit` finishes over several
   ticks without duplicates.
5. **Crash safety:** an exception inside the page transaction (after some rows are
   staged) leaves both the imports and the checkpoint at the previous value; the
   replay imports each report exactly once.
6. **Reconciliation, capped and restarted:** stub holds more than
   `rewalk_pages × limit` reports plus one inserted "behind" the forward checkpoint
   and positioned **beyond the first re-walk chunk**. Forward polls miss it. The
   re-walk spans several ticks; between ticks the app state is torn down and rebuilt
   (fresh poller over the same DB file) to model a restart; the walk resumes from
   `rewalk_cursor` (asserted: no page before the stored position is re-requested),
   imports the behind report once, completes, sets `last_full_walk_at` to its start
   time, and the forward `cursor` is byte-equal before and after. A new report added
   mid-re-walk is imported by the forward poll on the next tick, before the re-walk
   completes.
6a. **Re-walk errors:** a failed re-walk page keeps `rewalk_cursor` and
   `rewalk_started_at`; 400 on the re-walk clears only `rewalk_cursor`; 400 on the
   forward poll clears only `cursor`.
6b. **Stale in-flight poll:** the stub delays its response on an event; while it is
   held, the test (i) changes `ingest_config`, (ii) clears `ingest_source`, (iii)
   deletes the project (one case each). Releasing the response imports nothing, and
   leaves no `ingest_state` row (ii, iii) or only a row created later by a poll of the
   new generation (i). Same for a delayed **error** response (no stale `last_error`).
6c. **Deletion:** a project that has been polled (state row present) and imported
   zero reports deletes cleanly; a project whose imported reports were all
   soft-deleted deletes cleanly and removes its state row; a project with an active
   imported report still returns `has_reports`.
7. **Errors:** 400 clears the cursor; 401/503/502/timeout/network/3xx/malformed JSON
   each record the fixed error and leave the checkpoint; one failing project does not
   stop another; backoff skips and resets.
8. **Credential:** unset env var → no request, `credential_missing`.
9. **Secrecy:** planted token absent from logs (caplog at DEBUG), every DB table,
   `/health`, ingest status/run responses, report responses, and `last_error`.
10. **Mapping:** title derivation (long, multi-line, empty, whitespace), description
    cap, reporter fallback, email never stored anywhere (DB scan for the planted
    email), non-https attachment dropped.
11. **Pipeline hand-off:** an imported report is `submitted` and is returned by the
    worker's Stage 1 eligibility query.
12. **Config change** resets `ingest_state`; `ingest_source` cleared → project no
    longer polled.
13. **Disabled by default:** with `BUGALIZER_INGEST_ENABLED` unset no poller task
    starts; the manual run endpoint still works.
13a. **Auth and health:** both new endpoints 401 without a key when keys are set;
    public `/health` carries only `enabled`/`projects`/`failing` counts (no project id,
    error code or cursor in the body), and an ingest failure leaves `status` and the
    HTTP code unchanged.
14. Existing suite passes unchanged; CI green.

Acceptance on BOWIE (D-C, post-merge, recorded in
`docs/sonicgrid-ingest-acceptance.md` and the roadmap):

1. `SONICGRID_POLL_TOKEN` (same value as Vercel's `BUGALIZER_POLL_TOKEN`) and
   `BUGALIZER_INGEST_ENABLED=true` in BOWIE's env; sonicgrid project configured with
   `ingest_source=supabase` and the contract's `ingest_config`; restart.
2. `GET /projects/{id}/ingest` shows `credential_present: true`, and after one tick
   `last_ok_at` set, `last_error` null.
3. File a bug through sonicgrid's Report Bug dialog; within one interval it appears in
   Bugalizer's queue with `labels: ["sonicgrid"]`, the right `external_id`, and moves
   through Stage 1 to triage.
4. `POST /projects/{id}/ingest/run?full=true` → `imported: 0` (no duplicate).
5. Resolve the test bug in `/admin/bugs`; close Bugalizer's copy by hand.

## Risks

- **Sonicgrid contract drift.** The response model ignores unknown fields and treats a
  missing required field as a per-report skip, so an additive sonicgrid change is
  harmless; a breaking one surfaces as `malformed_response` on
  `GET /projects/{id}/ingest` (and as a `failing` count on `/health`).
- **Volume assumption.** The full re-walk is sized for sonicgrid's current handful of
  active reports. If active reports grow into the thousands, revisit (retain older
  cursors as re-walk starting points instead of walking from the beginning).
- **Two views that drift.** Sonicgrid resolution does not close Bugalizer's copy
  (out of scope by design). Operators close it by hand; noted in the acceptance doc.
- **Stale clone.** Imported reports localize against the BOWIE clone, refreshed by
  hand until Phase 9 (D-A).

## Closeout

Plan approved by codex at round 2; implementation approved at round 2
(2026-09-30). Full suite 363 passed on the approved tree. Real sonicgrid/BOWIE
acceptance pending post-merge (`docs/sonicgrid-ingest-acceptance.md`).

```
Phase report: sonicgrid-ingest — plan approved r2 · impl approved r2
  plan   2 rounds · 1 change request · 0 bounces
  impl   2 rounds · 1 change request · 0 bounces
  time   start→approve 21m 08s · implementation before first submit 10m 51s
         lead 6m 06s (2 spans, 2 unknown) · reviewer 4m 11s (4 spans)  (elapsed; includes relay wait)
  usage  no usage rows stored under this phase
```
