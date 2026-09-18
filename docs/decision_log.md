# Decision Log

This log tracks important decisions made during the project.

<!-- Add new decisions at the top in reverse chronological order -->

---

## 2026-09-17: Open-PR write policy and the single-process claim (Phase 8 / B2)

**Decision:** `POST /reports/{id}/open-pr` is the only code that writes to a remote, under a
tested write policy: the branch is always `fix/bugalizer-<report-id>` (server-computed); a guard
refuses the default branch and unprefixed refs; no force flag or `+` refspec on any fetch, push
or ref update; the commit is built detached in a throwaway worktree (no local branch, analysis
clone untouched); one branch and one PR per report, looked up (DB, then GitHub) before any
apply, commit or push; never merge. Every remote command names the canonical
`https://github.com/<owner>/<repo>.git` with an env-only credential, never `origin`.
Arbiter rulings (Greg, 2026-09-17): **R1** one fine-grained GitHub token in the service env
(`BUGALIZER_GITHUB_TOKEN`, single repo, Contents + Pull requests); **R2** a diff that no longer
applies fails as `409 diff_does_not_apply` and never triggers re-analysis; **R3** the branch is
built in a throwaway worktree, never in the analysis clone.

**Single-process assumption.** `fix_approved` is the claim, owned by a `claim_token` written
by compare-and-set. A request in flight registers its token in an in-process registry; a
`fix_approved` report whose token is not registered has lost its owner (process death, failed
rollback) and the next call adopts it by a CAS on the observed token. This is correct only
while one Bugalizer process serves a database, which `architecture.md` already requires
(single node; `--workers > 1` is unsupported because of the queue worker). Any future
multi-process deployment must replace the registry with a lease before it ships.

**Context:** B2 of the Aegis direction record (D4: "Open PR" is branch-only; a human merges).

**Alternatives Considered:**
- Build the branch in the analysis clone: mutates the checkout the pipeline reads (rejected, R3).
- GitHub App auth: more setup than one single-repo token for one repo; a later phase if needed.
- Time-based claim leases: need a clock policy and still misjudge slow calls; unnecessary in a
  single process.

**Decided By:** Greg (R1-R3) + claude (plan) + codex (APPROVE, open-pr plan round 3)

**Phase:** 8

---

## 2026-09-12: Two seams for the Aegis arc (Phase 7 / B0)

**Decision:** (1) The cloud tier accepts a per-request `llm` override (provider, model,
`api_key`, non-secret `key_ref`) on `POST /reports/{id}/analyze`. The key rides as an argument
of the in-process background task into `complete()` and is never stored, logged, or returned;
error text on the fix path is sanitized (`_safe_error_text`) and 422 bodies never echo the
request. `token_usage` records `key_source` (`request` | `env`) and the caller's `key_ref` so
spend stays attributable to the Aegis settings row that paid. (2) Projects carry an ingest
seam: `ingest_source` (`supabase` only for now) plus `ingest_config` holding `{url, table,
credential_env}`, the env var *name* only, validated as a pair against the merged row on PATCH.

**Context:** Ruled in the Aegis direction record (`~/projects/QA/docs/
bugalizer-integration-direction-2026-09-12.html`, D2 pull from Supabase, D3 key travels with the
request). B1 builds the Supabase poller on the ingest seam; A1 sends the override from the Aegis
Bugs page.

**Alternatives Considered:**
- Aegis writes the key into a per-project Bugalizer column (D3 option B): doubles the stored-key
  surface, rejected in the direction record.
- A secret-name blacklist on `ingest_config`: replaced by a closed schema (`extra="forbid"`,
  three declared fields) so there is no field a credential could ride in.
- A binary `key_source` flag only: cannot name which settings row paid; `key_ref` added at
  review.

**Decided By:** Human (jack) via the direction record; plan approved by codex (plan round 2).

**Phase:** 7 (bugalizer-revive, B0)

**Follow-ups:**
- B1: poller reads `ingest_config.credential_env` from the host environment at run time.
- A1: the engine proxy refuses to start when `/health` reports `auth_enabled: false`.

---

## 2026-07-03: Per-thread SQLite connections (dashboard parallel-poll crash)

**Decision:** `db.py` hands each thread its own SQLite connection for file databases
(generation-invalidated via `reset_conn()`); `:memory:` test databases keep the single shared
connection (TestClient serializes requests, and separate connections would see separate DBs).

**Context:** Found while live-smoking the Phase 5b dashboard on macOS. The app shared one global
`sqlite3.Connection` (`check_same_thread=False`) across FastAPI threadpool threads. The
dashboard polls with `Promise.all` — four concurrent requests — and on Python builds where
`sqlite3.threadsafety == 1` (e.g. macOS system libsqlite3) concurrent statements on a shared
connection corrupt the heap: observed as SIGSEGV inside `sqlite3Prepare` (fault address was
ASCII column-name bytes) and one `sqlite3.DatabaseError: database disk image is malformed`.

**Impact:** This code shipped on the Windows LAN box, whose service manager auto-restarts on
crash — meaning crashes could have been silently masked. Redeploying Phase 5b brings the fix
live; worth checking the manager's restart history afterwards.

**Alternatives Considered:**
- Serialize all DB access behind one lock: throttles the read-mostly dashboard workload.
- Connection pool: overkill for a single-process SQLite service; per-thread is the idiomatic fix
  under WAL (readers don't block; writes covered by busy timeout + `retry_on_locked`).

**Decided By:** claude (found + fixed in 5b Cycle 1) + codex (APPROVE, impl round 1)

**Phase:** 5b (Cycle 1)

---

## 2026-07-02: First LAN deployment live — hosting milestone COMPLETE

**Decision:** Bugalizer is deployed and hosted on the Windows GPU box via the LAN Service
Manager (`http://127.0.0.1:9000`) at `https://bugalizer.lan/` (Caddy reverse proxy →
`127.0.0.1:8090`, manager-supervised with auto-restart + boot start). This closes the
first/initial hosting phase. Remaining bugs and UI work are deferred to the next phase.

**Verified (this session):**
- Full local pipeline end-to-end against **real Ollama** (`qwen2.5-coder:7b`): validation →
  triage (structured output) → two-pass localization correctly pinpointed
  `db.py:reports_eligible_for_fix` + root cause. Health `database/ollama/worker` all green.
- Auth enforced (401 without `X-API-Key`, 200 with) — verified both on `127.0.0.1:8090` and
  through Caddy at `https://bugalizer.lan/`.
- Project create + git clone (HTTPS), dashboard served at `/`, 191/191 tests passing.
- LAN wiring: registered with the Service Manager; added the missing
  `127.0.0.1  bugalizer.lan` hosts entry (the manager does not auto-add hosts records).

**Bugs fixed en route (both matter on the Windows target):**
- Test env leak: `conftest.py` now clears `QA_LLM_*` so the suite exercises shipped defaults.
- **Windows coarse-clock bug:** `_now()` returned duplicate timestamps within a ~16 ms tick,
  breaking the strict-`>` `created_at` ordering the retry gate / `ORDER BY` rely on. `_now()`
  is now strictly monotonic per process.

**Config posture:** local LLM is the default for all stages (free, GPU); cloud (Anthropic) is an
explicit, cost-flagged opt-in. Pinned `BUGALIZER_FIX_PROVIDER=ollama` in `.env`.

**Known limitation / follow-ups (next phase — "bugs + UI work"):**
- **Stage 4 (fix proposals) unreliable on local Ollama** (7b *and* 14b): the model ignores the
  required schema and returns a generic JSON example. Options: opt into Anthropic (works, paid),
  or add Ollama schema-constrained structured output / reformat-retry loop to make local fixes
  viable. Reports currently settle at `triaged` with localization when fix fails.
- Triage (7b) is conservative — tends to return `clarification_needed` even for detailed reports.
- Full smoke test §4–6 (manual `hold`-mode analysis, cloud escalation, and reboot-survival from a
  second LAN machine) still pending real-hardware validation.
- Dashboard UI polish + open bugs: tracked for the next phase.

**Decided By:** Human (jack) + claude

**Phase:** 5 (§5.5 smoke test / deployment) — hosting milestone complete

---

## 2026-07-02: Adopt Phase 5 — Deployment Readiness & Queue Dashboard

**Decision:** The next phase is `docs/phases/phase-5-deployment-readiness.md`: worker retry
hardening, security defaults, per-report local/cloud analysis tier, minimal queue dashboard, and
Windows-LAN deployment packaging. Phase 6 (integrations) stays deferred.

**Context:** Full re-evaluation (2026-07-02) found Phases 1–4 implemented and 139 tests passing,
but four always-on-hosting blockers: unbounded no-backoff retries for Stages 3–4 (Stage 4 =
repeated paid Anthropic calls), no dashboard, no per-bug local-vs-cloud choice, and insecure
defaults with zero deploy tooling.

**Alternatives Considered:**
- Build the original Phase 5 (dashboard only): leaves the retry cost-risk and security defaults
  unaddressed while the service runs unattended.
- Jump to Phase 6 integrations: pointless until the service can be hosted reliably.

**Rationale:** The user's goal is permanent LAN hosting so other apps can submit bugs; reliability
and security blockers must land before (or with) visibility features.

**Decided By:** Human (jack) + claude

**Phase:** 5

**Follow-ups:**
- Queue the overdue Phase 4 tagteam review (Cycle 0).
- Decide the Fernet stub's fate in §5.2 (recommendation below).

---

## 2026-07-02: Retire the Fernet at-rest key-encryption plan (CONFIRMED, implemented 2026-07-03)

**Decision:** Remove the unimplemented Fernet stub (`settings.secret_key`, the unused
`projects.api_key_encrypted` column) rather than implementing it. Cloud credentials come from
environment variables only. **Confirmed in the Phase 5 plan review (round 2, codex APPROVE) and
implemented in Cycle 1 (§5.2):** `secret_key` dropped from `config.py` and `api_key_encrypted`
removed from the `projects` schema. Pre-existing databases keep the now-unused column harmlessly
(schema uses `CREATE TABLE IF NOT EXISTS`; nothing reads or writes it).

**Context:** The original architecture called for Fernet encryption of stored LLM API keys. It was
never implemented — only a config field and a DB column exist; `cryptography` is not even a
dependency. Meanwhile the working pattern is env-var credentials (`BUGALIZER_ANTHROPIC_API_KEY`).

**Alternatives Considered:**
- Implement Fernet properly: adds a dependency, key-management burden (where does the Fernet key
  live? …an env var), and code for no current gain.
- Leave the stub: misleading — the schema column advertises encryption that doesn't exist.

**Rationale:** Single-user LAN service; the threat model doesn't include multi-tenant stored
secrets. Env-var secrets are the simplest honest posture. Revisit if per-project cloud keys
become a real feature (Phase 6 integrations).

**Decided By:** claude (recommendation) + codex (APPROVE, Phase 5 plan round 2)

**Phase:** 5 (§5.2)

**Follow-ups:**
- ~~Remove `secret_key` setting and `api_key_encrypted` column reference in the §5.2 change.~~ Done 2026-07-03 (Cycle 1).

---

## 2026-07-02: Migrate to uv

**Decision:** Standardize on `uv` for dependency and environment management: `uv sync --dev`,
`uv run pytest`, dev deps in `[dependency-groups]`, `requirements.txt` removed in favor of
`uv.lock`, `requires-python >= 3.12`.

**Context:** The repo carried both a stale 4-line `requirements.txt` and pyproject extras; the
handoff tooling migration (tagteam) landed as a dev dependency and needed a lockfile.

**Alternatives Considered:**
- Keep pip + venv: no lockfile, drift between requirements.txt and pyproject.

**Rationale:** Reproducible installs on both the Mac dev machine and the Windows deployment
target; single source of truth in `pyproject.toml` + `uv.lock`.

**Decided By:** Human (jack) + claude

**Phase:** Housekeeping (Phase 5 §5.0)

**Follow-ups:**
- Deployment docs (§5.5) must use `uv` commands.
