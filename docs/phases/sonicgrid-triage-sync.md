# Phase 11: sonicgrid-triage-sync (B3)

## Summary

B3 of the Aegis Bugalizer arc. Direction: the Aegis repo's
`docs/bugalizer-sonicgrid-triage-direction-2026-09-30.html` (rulings E1 to E6, Jack,
2026-09-30; on the QA branch `docs/bugalizer-sonicgrid-triage-direction`). Sonicgrid
users see what Bugalizer made of their bugs and sonicgrid admins act on them, without
BOWIE accepting a single inbound connection.

The contract is sonicgrid S2's `documentation/BUGALIZER-TRIAGE-ENDPOINTS.md` (sonicgrid
`main`, `9ce7fe50`, PR #618), proven in production by the S2 hosted acceptance walk on
2026-09-30. Everything below codes against that document. Where this plan says "the
contract" it means that file, and where the two disagree the contract wins.

Three calls, all BOWIE → sonicgrid, behind a new write-scoped token
(`SONICGRID_TRIAGE_TOKEN`, distinct from B1's `SONICGRID_POLL_TOKEN`):

- `PUT /results/{bugId}`: push the full current picture of one sonicgrid-sourced report.
- `GET /actions`: list open admin actions (walked in full every poll, cursor never stored).
- `POST /actions/{actionId}`: advance an action (`claimed`, then `done` / `failed` /
  `refused`).

Depends on Phase 10 (B1: imported reports carry `external_id`) and Phase 8 (B2:
`open_pull_request`). Uses Phase 7's `key_ref` for spend attribution. Phase 12 (B4,
per-user keys) is out of scope: in B3 a cloud run always uses BOWIE's own configured
key and is only allowed for allowlisted sonicgrid users (E4).

**Revision r2** (codex plan r1): `analyze_local` is checked for a paid provider too, and
every LLM action runs on a provider/model pinned when it is authorized (D-A, §4a); one
per-project tick lock plus an atomic ledger reservation means two ticks can never both
dispatch an action, and one sync consumer per sonicgrid source is enforced (§1, §3);
stage rows carry the action id that started them, so recovery evidence is exact;
`fix_and_open_pr` persists `fix: done` and its proposal id before the PR step, and a
timed-out task can neither start the PR step nor overwrite the posted outcome (§3a);
terminal acknowledgements are drained from the ledger regardless of the open-only listing
(§3); the fingerprint covers the whole payload including `bugalizerUpdatedAt`, and a
pending push resends the same revision and payload (§2).

**Revision r3** (codex plan r2): a B3 `analyze_local` runs only on `ollama` and is
refused otherwise for every requester, so there's no paid local path left to attribute
(D-A, §4); the recovery rule's "never re-dispatch" set is a separate predicate,
`no_auto_retry`, which always includes `analyze_cloud` whatever its provider (§3).

### Decisions proposed in this plan (for codex / Jack)

- **D-A Which actions the cloud allowlist gates.** A stage is **paid** when it resolves
  to a provider other than `ollama`. `analyze_cloud` is always cloud-tier and always
  needs the allowlist, as the contract says. `analyze_local` from sonicgrid means local:
  if the project's local provider (`resolve_local_llm`, which accepts `anthropic`) is
  paid, it is **refused for every requester**, allowlisted or not, with a message saying
  the project's local stages aren't configured for a local model. The fix step of
  `fix_and_open_pr` needs the allowlist when the resolved Stage 4 provider is paid. For
  recovery, `no_auto_retry = kind == analyze_cloud OR a paid fix step` (§3). On BOWIE today (`ollama` everywhere) Dan can
  "fix and open PR" and "Analyze (local)" on the local models, and his "Analyze (cloud)"
  is refused. That matches the direction's acceptance line ("Dan's cloud request is
  refused with the reason shown").
- **D-B The push revision is issued by B3, not read from `bug_reports.updated_at`.**
  `updated_at` is not bumped by analysis rows or PR records (`analysis_create`,
  `fix_proposals` updates), so it misses changes the push must carry. B3 fingerprints
  the payload instead and issues `revision = max(last_revision + 1, now in epoch µs)`
  whenever the fingerprint changes (§2).
- **D-C Spend attribution.** A cloud run started by a sonicgrid action records
  `key_source = "env"`, `key_ref = "sonicgrid:<userId>"` in `token_usage`, via a new
  internal-only parameter on `propose_fix`. The public API's rule ("`key_ref` requires
  `api_key`") is unchanged. B4 will later record `key_source = "request"` for a user's
  own key. Alternative if codex prefers keeping `key_ref` strictly "which key": a new
  nullable `token_usage.requested_by` column, grouped the same way.
- **D-D Requester email is not stored.** As in B1 (D-B there), the email in
  `requestedBy` is used in memory for the allowlist check only. The ledger stores
  `requestedBy.userId`.
- **D-E Stage entry points return an outcome.** `propose_fix`, `process_localization`
  and `run_local_analysis` return `None` today, so B3 cannot tell "ran and failed" from
  "never claimed" from "deferred on a precondition". They will return a small outcome
  value (§4). Existing callers ignore the return value, so they don't change.

## Scope

In:

1. A **triage-sync task** (own `asyncio.Task`, off by default) that runs the action walk
   and the results push every tick.
2. **Results push**: payload builder, fingerprint and revision, retry-until-200.
3. **Action executor** for all six kinds, with the contract's refused/failed semantics and
   the `fix_and_open_pr` step rules.
4. **Durable action ledger** and the contract's recovery table on every poll.
5. **Cloud allowlist** (`BUGALIZER_SONICGRID_CLOUD_USERS`) and per-user `key_ref`
   attribution.
6. **Token secrecy**, matching Phases 7, 8 and 10.
7. **Operator surface**: sync status (API key) and aggregate counts on `/health`.

Out:

- Per-user cloud keys (B4 / Phase 12). No key ever arrives in an action in B3.
- Any change to sonicgrid's report `status` (the contract forbids it).
- Dashboard changes. Results and actions are visible in sonicgrid, and the ledger through
  the API.
- Private-repo refresh (Phase 9). Fixes and PRs still work against the hand-refreshed
  BOWIE clone, so open-pr's `diff_does_not_apply` refusal is the expected outcome when
  the clone is stale.
- A budget cap. The contract says Bugalizer has none, and the allowlist is the only
  control (E4).

## Technical Approach

### 1. The sync task (`src/bugalizer/sync/triage_sync.py`)

- Started and stopped in `main.py` `lifespan` next to the queue worker and the ingest
  poller. Gated on `BUGALIZER_TRIAGE_SYNC_ENABLED` (default **false**, so dev and tests
  are unchanged).
- Tick interval `BUGALIZER_TRIAGE_SYNC_SECONDS` (default 15, the contract's "about every
  15 seconds"). Each tick, per project with sync configured: (a) walk and execute
  actions, (b) push changed results. A failure in one project, or in one half of a tick,
  never stops the rest.
- **Per-project configuration** reuses B0's ingest seam. `ingest_config` gains an optional
  `triage_credential_env` (BOWIE: `"SONICGRID_TRIAGE_TOKEN"`). Absent means sync is off
  for that project. The base URL is the S0 poll URL's parent, as the contract says
  (`…/api/bugalizer/bug-reports` → `…/api/bugalizer`), derived and not configured
  separately, so the two can't drift. A project PATCH validates the new key the same way
  B0 validates `credential_env`.
- **One tick at a time per project.** A per-project `asyncio.Lock` is held for the whole
  tick, by both the background loop and `POST /projects/{id}/triage-sync/run`. A manual
  run that finds the lock held answers `409 tick_in_progress` and does nothing. Bugalizer
  is a single process by design (WAL SQLite, no horizontal scaling), so this lock plus
  the ledger reservation in §3 makes B3 the contract's single consumer.
- **One consumer per sonicgrid source.** `GET /actions` lists actions for every bug on
  that sonicgrid, not per project. A project PATCH that would give two projects the same
  derived triage base URL with `triage_credential_env` set is rejected (`409
  triage_source_in_use`). If such a pair already exists (for example, set directly in
  the DB), the tick skips both and records `duplicate_triage_source`. Running two
  Bugalizer instances against one sonicgrid is unsupported and documented as such in the
  deploy notes.
- HTTP: `httpx.AsyncClient`, no redirects, timeout `BUGALIZER_INGEST_TIMEOUT_SECONDS`.
  The module exposes an `http_transport` seam for tests, like the poller.
- Status handling, the same for all three routes. `401` / `503`: record `unauthorized` /
  `source_not_configured` and skip the rest of this project's tick. `502`, network
  error, timeout: record and retry next tick. Project-level backoff `min(2^failures, 32)`
  ticks applies to `401`/`503`/network only, so one bad payload doesn't back off the
  whole project.

### 2. Results push

**Which reports:** every report in a sync-enabled project with `external_id` set.

**Payload** (built by a pure function in `src/bugalizer/sync/results.py`, field by field
against the contract's table; every object strict, no extra keys):

| Contract field | Source |
|---|---|
| `public.pipelineStatus` | `bug_reports.status` (`BugStatus` matches the contract's vocabulary) |
| `public.summary` | latest completed `triage` analysis `result.summary`, truncated to 2,000 |
| `public.severity` | `bug_reports.severity` (triage writes it onto the report); null if not in the vocabulary |
| `public.prUrl` | the recorded PR's `pr_url` (B2) |
| `admin.analysisMode` | `bug_reports.analysis_mode` |
| `admin.localized` | a completed localization exists |
| `admin.category`, `admin.triageConfidence` | triage `result.category` (≤ 50), `result.confidence` (clamped 0..1 or null) |
| `admin.rootCauseHypothesis`, `admin.candidateFiles` | latest completed localization: `pass2.root_cause_hypothesis`; `pass1.candidate_files` (≤ 50 entries, fields truncated) |
| `admin.rootCause`, `admin.explanation`, `admin.diff`, `admin.fixConfidence` | latest `fix_proposals` row; `diff` null if over 256 KB (a truncated diff is worse than none) |
| `admin.pr` | `{number, branch}` from the recorded PR |
| `bugalizerUpdatedAt` | the newest timestamp among the report and its analysis / proposal rows |

Values that fail a contract limit after truncation (for example a non-numeric
confidence) become null rather than causing a `400`. A unit test checks the builder's
output against a schema that mirrors the contract's limits.

**Revision (D-B).** New table `triage_results (report_id PK, project_id, fingerprint,
revision, payload JSON, acked_fingerprint, last_status, last_error, pushed_at)`. Each
tick: build the payload, `fingerprint = sha256(canonical JSON of the whole payload
except revision)`, which includes `bugalizerUpdatedAt`, so a newer analysis with
identical output still pushes.

- Fingerprint differs from the stored `fingerprint`: issue a new revision greater than
  the last one issued (`max(revision + 1, epoch_us(now))`) and store fingerprint,
  revision and payload together in one write **before** the PUT.
- Stored `fingerprint` not yet acked (a failed or interrupted push): resend the stored
  payload with the stored revision, so one revision always means one payload.
- `200`, either `applied` value: set `acked_fingerprint = fingerprint` and stop retrying
  (contract caller rule).
- `200 applied:false` with a `storedRevision` at or above ours (only possible if the
  ledger was lost): mark it acked; the next change is issued above `storedRevision`.

- `400` / `413` / `404`: record `last_error`. Don't retry the same fingerprint. A new
  fingerprint is pushed normally.
- Push budget: at most `BUGALIZER_TRIAGE_PUSH_PER_TICK` (default 20) pushes per project
  per tick, oldest change first, so a first sync of a backlog is spread over ticks.

### 3. Action walk and ledger

Each tick: `GET /actions?limit=100` from no cursor, following `next_cursor` to null
(contract walk rule; cursor never stored). Each action is resolved to a report by
`(project_id, external_id = bugId)`. An action whose bug isn't imported yet is left
**pending** and counted in the status endpoint. Ingest normally imports it within one
B1 interval.

New table `triage_actions` (one row per sonicgrid action id, created only by B3):

```
action_id TEXT PK, project_id, report_id, kind, params JSON, requested_by_user_id,
phase TEXT     -- reserved | intent | dispatched | fix_done | finished
no_auto_retry INTEGER, pinned_llm JSON,  -- fixed at authorization (§4a)
reserved_at, intent_at, dispatched_at, finished_at,
outcome TEXT   -- done | failed | refused (set once, at finished; never overwritten)
message, steps JSON, fix_proposal_id, pr_url, attempts INTEGER,
terminal_acked INTEGER, late_outcome TEXT
```

**Ledger first, listing second.** When a listed action already has a ledger row, the
row's `phase` decides what happens; the listed state only matters where noted below.

**Reservation (the single exclusive step on Bugalizer's side).** In the tick, under the
per-project lock (§1), a pending action with no ledger row is taken only when an executor
slot (below) is free for it: `INSERT … phase='reserved'` in the same DB transaction that
counts the slot, and the slot plus the report are held in memory from that point. If the
insert finds an existing row, the action is not new and goes to the ledger rules. Then
POST `claimed`:

- `200` (`changed` true or false): set `phase='intent'` with `intent_at`, then dispatch.
  Only the holder of the reservation ever reaches this point, so a repeated
  `changed:false` (contract scenario 6) dispatches exactly once.
- `409`: delete the reservation and release the slot; don't run.
- No answer: keep `reserved`; the next tick re-POSTs `claimed`. Nothing has been
  dispatched while `phase='reserved'`, so this is safe for every kind.

**Per ledger phase, every tick** (for actions in the listing; finished rows also drain
separately, below):

- `reserved` (listed `pending` or `claimed`): POST `claimed` again, as above.
- no row, listed `claimed` (a crash right after the claim, before the reservation
  committed; or a lost claim acknowledgement): create the row as `reserved`, then the
  same `claimed` POST and dispatch path. Contract scenario 6.
- `intent`, no running task (crash between recording and dispatching, or a dispatch that
  timed out): look for **this action's** evidence, meaning stage rows tagged with this
  `action_id` (§4a), not just any rows on the report. Found: adopt it and follow it. Not
  found and not `no_auto_retry`: dispatch again. Not found and `no_auto_retry`
  (`analyze_cloud` on any provider, or a `fix_and_open_pr` whose fix step is paid and not
  yet `fix_done`): finish
  `failed`, "dispatch could not be confirmed; not retried automatically to avoid a
  second cloud charge; request again" (contract scenario 7).
- `dispatched` with a running task: wait, up to the time bound.
- `dispatched` with no running task (restart): derive the outcome from rows tagged with
  this `action_id`; none, and the report is out of the stage's claim state: the
  `intent` rule above.
- `fix_done` (`fix_and_open_pr` only): run the **open-pr step only**, with the stored
  `fix_proposal_id`. Never re-enter `propose_fix` (§3a).
- `finished`: nothing to execute; the drain posts it.

**Terminal drain, independent of the listing.** Every tick, before the walk, each ledger
row with `phase='finished'` and `terminal_acked=0` is POSTed with its stored
`outcome`/`message`/`steps`, whether or not the action still appears in `GET /actions`
(a committed terminal POST whose response was lost has left the open-only listing).
Result handling:

- `200`, `changed` true or false: set `terminal_acked=1`.
- `409` with `currentState` equal to our outcome: also acked.
- `409` with another state, or `404`: set `terminal_acked=1` with `last_error` recorded,
  and count it on the status endpoint. This is a contract breach, so it's surfaced and
  not retried forever.

**Time bound**: `BUGALIZER_TRIAGE_ACTION_TIMEOUT_MINUTES` (default 45, measured from
`intent_at`; a 14b local localization on BOWIE takes minutes, not seconds). Documented
in the deploy notes, as the contract requires. Past the bound the row is finished
`failed` ("timed out after N min"), then §3a's late-completion rule applies.

**Executor slots.** LLM-bound kinds (`analyze_*`, `fix_and_open_pr`) run as tracked
`asyncio` tasks, at most `BUGALIZER_TRIAGE_MAX_CONCURRENT` (default 1: one GPU) at a
time, and at most one action per report, whatever its kind. `open_pr`, `set_mode` and
`close` run inline in the tick, but still take the per-report reservation. A slot and
its report are released only when the task has actually exited, not when the action is
finished, so a timed-out task still counts against the bound until it stops.

### 3a. Compound actions and late completion

- **`fix_and_open_pr` checkpoints between steps.** When the fix step returns `proposed`,
  B3 writes `phase='fix_done'`, `fix_proposal_id`, and the fix step's entry in `steps`
  in one DB write, **before** calling open-pr. After a crash at any later point, recovery
  resumes at the open-pr step with that id. `propose_fix` is never called a second time
  for the action, so the "already_proposed" refusal can't misreport a fix this action
  made. Open-pr is idempotent per report and resumes at the PR step on a branch it
  already pushed (B2), so re-running the PR step after a crash after the branch push is
  safe and spends nothing.
- **A finished row is never rewritten.** `outcome`, `message` and `steps` are written
  once, by a conditional update `WHERE phase != 'finished'`. A task that completes after
  its action was finished by the time bound loses that update. Its outcome goes to
  `late_outcome`, for the operator only, and it **does not start the next step**: before
  open-pr, the task re-reads its row and stops unless `phase='fix_done'` still holds and
  the row is not finished. A late fix proposal stays in Bugalizer, where an admin can
  request `open_pr` for it.

### 4. Executor (`src/bugalizer/sync/actions.py`)

B3 calls Bugalizer's internals in process, never its own HTTP API (no API key needed, and
no BackgroundTasks to lose track of). The preconditions the endpoints check move into
small shared functions that the endpoints and B3 both call, so the 409/422 rules can't
drift:

- `check_local_analysis(report)`, `check_cloud_analysis(report)` (status, completed and
  SHA-fresh localization), extracted from `api/reports.py` `analyze_report`.
- `check_status_transition(report, target)`, extracted from the status PATCH.

**Outcomes (D-E).** `propose_fix` returns `FixOutcome(kind, proposal_id=None,
error=None)` where `kind` is one of `proposed`, `failed`, `deferred`, `not_claimed`,
`already_proposed`. `process_localization` / `run_local_analysis` return `completed`,
`failed` or `not_claimed`. Each existing return path maps one to one; there's no
behaviour change for the worker or the endpoint.

| Kind | Refused (nothing ran) | Runs | Done / failed |
|---|---|---|---|
| `analyze_local` | terminal status; `check_local_analysis` fails; pinned triage or localize provider not `ollama` (any requester) | `run_local_analysis(pinned=…, trigger_ref=action_id)` | `completed` → done; `failed` → failed; `not_claimed` → refused |
| `analyze_cloud` | not allowlisted (always, D-A); `check_cloud_analysis` fails | `propose_fix(pinned=…, attribution_ref=…, trigger_ref=action_id)` | `proposed` → done (message names the proposal); `failed` → failed; `deferred` / `not_claimed` / `already_proposed` → refused |
| `set_mode` | invalid `params.mode` | `report_update_fields(analysis_mode=…)` | done; DB error → failed |
| `close` | `check_status_transition` fails | `report_update_status(closed)` | done; DB error → failed |
| `open_pr` | `consents.repoWrite` not true; the contract's refused codes | `open_pull_request(report_id, None)` | 201 / 200 → done (`ref` the PR URL in the message); `in_progress` → stay claimed; `github_error`, `git_error`, other → up to 3 attempts across ticks, then failed with what is known (for example "branch `fix/bugalizer-<id>` pushed; PR creation failed: …") |
| `fix_and_open_pr` | `consents.repoWrite` not true; fix would be paid and (`consents.cloudSpend` not true, or not allowlisted) | fix step: the same `propose_fix` call as `analyze_cloud` (allowlist only when paid); checkpoint `fix_done` (§3a); then open-pr with **that** `proposal_id` | the contract's five step combinations; open-pr runs only after `fix: done` |

- `already_proposed` (a proposal already exists for the latest localization) refuses
  the fix step, because the contract counts only a proposal *this* action made. The
  message points the admin at `open_pr`.
- Refusal messages name the reason in plain words ("cloud analysis is limited to
  allowlisted users"). For `pr_exists` / `pr_unattributed` they include the existing
  `pr_url`.
- Messages and step messages are capped at 2,000 characters and pass through the same
  key scrubbing as Stage 4 errors (`_safe_error_text`).

**Allowlist.** `BUGALIZER_SONICGRID_CLOUD_USERS`: comma-separated emails, compared after
case-folding and trimming. Empty means no one gets cloud. "Paid" for D-A: any provider
the action would use, as pinned in §4a, other than `ollama`.

**Attribution (D-C).** `propose_fix` gains a keyword-only `attribution_ref: str | None`,
used only when no request key is present: `record_token_usage(key_source="env",
key_ref=attribution_ref)`. Only B3 passes it.

### 4a. Pinned configuration and action tags

- **Pinned at authorization.** When an action moves to `intent`, B3 resolves every LLM
  stage it will run: `resolve_local_llm(project, "triage")` and `(…, "localize")` for
  `analyze_local`; `resolve_fix_llm(project)` for `analyze_cloud` and the fix step. It
  decides the allowlist, the `analyze_local` refusal and `no_auto_retry` from those
  values, and stores them in
  `pinned_llm`. The stages then run with exactly those values: `run_local_analysis`
  threads an optional `pinned` provider/model through `process_triaged` /
  `process_localization` to `triage_report` / `localize_report`, which already take
  explicit `provider`/`model` arguments ("explicit arguments win"). `propose_fix`
  receives them as an `LLMOverride` with provider and model only, so there's no request
  key and attribution stays `key_source=env`. A project PATCH after authorization can't
  turn an approved local run into a paid one. A re-dispatch after recovery uses the
  stored `pinned_llm`, not a fresh resolution.
- **Action tags.** New nullable column `trigger_ref` on `analyses` and `fix_proposals`.
  Every stage row written during a B3 run carries the action id (completed and failed
  triage/localize/fix rows, and the proposal). Stage functions take a keyword-only
  `trigger_ref`; the worker and the HTTP endpoints never pass it, so their rows keep
  null. Recovery evidence is "rows with `trigger_ref = action_id`", which a manual
  dashboard run or a worker run on the same report can never match. For open-pr, the
  evidence is the report's recorded PR for the stored `fix_proposal_id` (B2 keeps one
  PR per report).

### 5. Token secrecy

`SONICGRID_TRIAGE_TOKEN` is resolved per request through `resolve_env_credential`, never
cached, never in a URL, log line, error, ledger row or response. Tests reuse the Phase 10
pattern: a sentinel token value is asserted absent from captured logs, every
ledger/results row, the status endpoint and `/health`, including on `401`/`502`/timeout
paths. The requester email is asserted absent from logs and tables (D-D).

### 6. Operator surface

- `GET /projects/{id}/triage-sync` (API key): enabled/configured, last tick, last error
  per route, counts of pending/claimed/finished actions, results pending push, last push
  errors (report ids only).
- `POST /projects/{id}/triage-sync/run` (API key): one tick now, for the acceptance walk.
- `/health` gains a `triage_sync` block with counts only (`projects`, `failing`), like
  B1's ingest block.
- `scripts/windows/configure-sonicgrid-triage.ps1`: sets `triage_credential_env` on the
  sonicgrid project, checks the env vars are present (without printing them), runs one
  tick, prints `CONFIGURED`. Modelled on B1's configure script.

## Implementation notes (impl round 1)

Where the code settles details the plan left open, or differs from it in detail:

- **Stage names.** The localization rows' phase is `localization` (the plan wrote
  "localize" in places); pins are stored as `{"triage": [p, m], "localization": [p, m]}`
  or `{"fix": [p, m]}`.
- **When terminals are posted.** Inline kinds finish inside the tick and are posted by
  the drain at the end of the same tick. LLM tasks finish between ticks and are posted
  by the next tick's first drain (at most one tick later).
- **Claim answers.** `409` or `404` on `claimed` deletes the reservation (nothing ran);
  any other non-200 keeps it `reserved` for the next tick.
- **Local outcomes.** `ALREADY_FRESH` (localization already current for HEAD) and
  `NO_REPO` map to `refused` (nothing ran); `TRIAGE_ONLY` (triage ran and asked for
  clarification, so no localization) maps to `failed`.
- **Open-pr step.** `in_progress` keeps the action claimed without counting an attempt;
  `github_error`, `git_error` and anything unexpected count, and the third attempt
  (across ticks) finishes `failed`. Any open-pr code outside the contract's refused list
  takes that failed path. Inline `close` after a crash counts an already-closed report as
  `done`.
- **Terminal drain answers.** `409` whose `currentState` equals ours counts as acked.
  Another `409`, a `404` or a `400` is acked with `terminal_error` (contract breach,
  counted on the status endpoint, logged by action id).
- **Tick lock.** The background loop waits for the per-project lock; only the manual run
  answers `409 tick_in_progress`.
- **DB writes** in the sync are single-statement calls with no await inside, so they are
  not wrapped in `db_write_lock`. Slot check, ledger insert and hold run with no await in
  between, which makes the reservation atomic for every coroutine on the loop.
- **`triage_credential_env`** is excluded from the B1 poll identity, so adding or removing
  it does not bump `ingest_generation` or reset the poll checkpoint. `validate_ingest_pair`
  dumps with `exclude_none`, so a config without it stays the exact B1 dict.
- **Tests** replace the three stage entry points with fakes that write `trigger_ref`-tagged
  rows like the real ones (`tests/test_triage_sync.py`). The real stages' new parameters
  (pin, attribution, tags, unchanged untagged behaviour) are tested directly against a
  mocked LLM in the same file.
- **Known, out of scope:** `tests/test_fix_proposer.py:536` sets `settings.default_fix_model`
  without restoring it, so `test_analysis_mode.py::test_resolve_fix_llm_default_project_is_cloud`
  fails when that file runs after it out of the default order (it fails the same way on
  `main`). The default full-suite order passes.

## Files

- New: `src/bugalizer/sync/__init__.py`, `sync/triage_sync.py` (task, HTTP, walk,
  recovery), `sync/results.py` (payload builder, fingerprint), `sync/actions.py`
  (executor, allowlist)
- `src/bugalizer/db.py`: `triage_results`, `triage_actions` tables + migrations + CRUD
  (atomic reservation, conditional finish); nullable `trigger_ref` on `analyses` and
  `fix_proposals`; cascade/cleanup on project delete; evidence queries by `trigger_ref`
- `src/bugalizer/pipeline/fix_proposer.py`, `pipeline/orchestrator.py`,
  `pipeline/triage.py`, `pipeline/localizer.py`: outcome returns (D-E), `attribution_ref`
  (D-C), `pinned` provider/model and `trigger_ref` keyword arguments (§4a)
- `src/bugalizer/api/reports.py`: precondition checks extracted (behaviour unchanged)
- `src/bugalizer/api/projects.py`: `triage_credential_env` validation; triage-sync
  endpoints
- `src/bugalizer/config.py`: the new `BUGALIZER_TRIAGE_*` and
  `BUGALIZER_SONICGRID_CLOUD_USERS` settings
- `src/bugalizer/main.py`: lifespan start/stop, `/health` block
- `tests/test_triage_sync.py`: against a fake sonicgrid (`httpx.MockTransport`) that
  implements the contract's state machine, step validation and revision rule
- `scripts/windows/configure-sonicgrid-triage.ps1`, `docs/sonicgrid-triage-acceptance.md`,
  `.env.example`, `docs/deploy-windows.md`, `docs/roadmap.md`, `CLAUDE.md`

## Success Criteria

Automated (fake sonicgrid; LLM and GitHub mocked as today):

1. **Push**: a sonicgrid-sourced report is pushed once; an unchanged report isn't pushed
   again; a new analysis row (which doesn't touch `updated_at`) causes a push with a
   higher revision, including one whose output is identical and only
   `bugalizerUpdatedAt` moved; an interrupted push is resent with the same revision and
   payload; `applied:false` stops retrying; `400`/`404`/`413` aren't retried for
   the same fingerprint; a report without `external_id` is never pushed.
2. **Payload**: matches the contract schema (strict keys, limits); an oversized diff
   becomes null; a non-vocabulary severity becomes null.
3. **Walk**: follows `next_cursor` to null every tick and never stores it; a pending
   action behind claimed ones is reached in the same tick.
4. **Each kind** reaches the state the contract's semantics table gives, including the
   five `fix_and_open_pr` combinations and `skipped` after the first non-done step;
   open-pr gets the fix step's proposal id; `already_proposed` refuses the fix step.
5. **Allowlist and pinning**: `analyze_cloud` from a non-allowlisted user is refused,
   the message says why and nothing runs; the same user's `analyze_local` runs on an
   `ollama` project; with `project.llm_provider=anthropic` an `analyze_local` is refused
   for an allowlisted and a non-allowlisted user alike, with no LLM call;
   `fix_and_open_pr` from that user runs on an `ollama` fix provider and is refused on a
   paid one; a project PATCH to a paid provider between authorization and stage
   execution doesn't change the provider the stage calls (the LLM mock asserts the
   pinned provider/model).
6. **Recovery**: each contract scenario 1 to 7 as a test, including a crash between
   intent and dispatch (`analyze_local` re-dispatched; `analyze_cloud` failed with no
   second LLM call, tested with both an `ollama` and a paid pinned fix provider), a lost claim ack, and a timeout. Plus: evidence from a manual
   dashboard run on the same report isn't adopted (no `trigger_ref` match); a crash after
   `fix_done` and before open-pr resumes the PR step with the stored proposal id and
   never calls `propose_fix` again; a crash after the branch push resumes at the PR
   step; a timeout followed by a late fix completion leaves the posted `failed` intact,
   records `late_outcome`, and never calls open-pr.
7. **Concurrency and ownership**: overlapping background and manual ticks (the manual one
   gets `409 tick_in_progress`); repeated `claimed` → `changed:false` answers dispatch
   exactly once with one ledger outcome; at most `MAX_CONCURRENT` LLM tasks and one
   action per report, with the slot held until a timed-out task exits; extra actions stay
   pending; a second project with the same triage source is rejected.
7a. **Terminal drain**: a terminal POST committed by the fake but with its response
   dropped, followed by an empty action listing and a process restart, is re-POSTed
   from the ledger, answered `changed:false`, and marked acked.
8. **Attribution**: a B3 cloud run writes `key_source=env`, `key_ref=sonicgrid:<userId>`,
   visible in the usage attribution breakdown.
9. **Secrecy**: sentinel token and requester email absent everywhere (§5).
10. **No regression**: sync off by default; the existing suite passes unchanged; the
    analyze/status endpoints keep their responses (shared checks).

On BOWIE (the direction's exit criterion, walked by Jack with Dan):

- Jack requests "Analyze (cloud)" in sonicgrid and sees the result there within one
  action interval plus the run time.
- Dan's "Analyze (local)" runs; Dan's "Analyze (cloud)" is refused with the reason shown.
- "Fix and open PR" ends with a PR on `fix/bugalizer-<id>` that a human merges on GitHub,
  or with an honest refusal (for example `diff_does_not_apply` against the stale clone).
  That depends on B2's token from the repo owner, which is still pending (Phase 8).
- A reporter who is not an admin sees status, summary, severity and the PR link only.

## Risks

- **Cloud spend.** Only through `analyze_cloud` or a paid fix step, and only for
  allowlisted users. BOWIE's Stage 4 is `ollama` today, so there's no paid run until
  that changes.
- **Stale BOWIE clone** (Phase 9). Fix-and-PR will often end `refused:
  diff_does_not_apply`. That's correct behaviour, and it's visible in sonicgrid.
- **GPU contention.** One LLM action at a time, sharing Ollama with the queue worker.
  Actions queue visibly as pending.
- **Private code in sonicgrid's DB.** Diffs and file paths go to `bugalizer_results`.
  Sonicgrid filters on read (E3). B3 sends every field, as the contract allows.
- **Clock regressions across restarts.** The revision is `max(last + 1, now)`, so a
  backwards clock never re-issues a lower revision.
