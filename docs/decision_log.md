# Decision Log

This log tracks important decisions made during the project.

<!-- Add new decisions at the top in reverse chronological order -->

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
