# Decision Log

This log tracks important decisions made during the project.

<!-- Add new decisions at the top in reverse chronological order -->

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

## 2026-07-02: Retire the Fernet at-rest key-encryption plan (pending §5.2 confirmation)

**Decision:** Recommend removing the unimplemented Fernet stub (`settings.secret_key`, the unused
`projects.api_key_encrypted` column) rather than implementing it. Cloud credentials come from
environment variables only. Final confirmation happens in Phase 5 §5.2 review.

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

**Decided By:** claude (recommendation) — pending codex review in Phase 5 Cycle 1

**Phase:** 5 (§5.2)

**Follow-ups:**
- Remove `secret_key` setting and `api_key_encrypted` column reference in the §5.2 change.

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
