# Phase 7: bugalizer-revive (B0)

## Summary

First phase of the Aegis Bugalizer arc, as recorded in the Aegis repo at
`docs/bugalizer-integration-direction-2026-09-12.html` (Phase 46 there; rulings
D1 to D6 by Greg on 2026-09-12). B0 brings this repo back to a state a reviewer
can work in and adds the two seams the later phases build on: a per-request
provider, model and key override on the cloud analyze call (D3) and a
per-project ingest-source setting that B1 `sonicgrid-ingest` will read.

Surveyed 2026-09-12: last commit 2026-07-03; no CI; README says 191 tests,
CLAUDE.md says 201, the suite has 201 and passes in 6 s; service live on BOWIE
at :8090. Tree cleanup (scope item 1) was done in the working tree later the same
day and is staged, uncommitted; the plan-init baseline predates it.

## Scope

**In scope**

1. **Tree and tooling.** Done in the working tree 2026-09-12 (staged, commits with the
   phase): tagteam upgraded to 3.12.0 (uv tool and the project dev-dep pin, `>=3.12.0`,
   relocked); `tagteam upgrade` run, which removed the vendored
   `.claude/skills/handoff/SKILL.md`, refreshed `templates/`, `docs/checklists/` and
   `docs/workflows.md`, and created `AGENTS.md`; `.handoff-session.json` and
   `.tagteam/.write.lock` untracked and `.tagteam/` gitignored; `/handoff` pointers in
   `CLAUDE.md`, `README.md`, `docs/roadmap.md` and the review skill now name
   `/tagteam:handoff`. Existing `CLAUDE.md` content preserved. The impl submission
   describes this from the actual staged diff, not from the survey.
2. **CI.** `.github/workflows/ci.yml`: on push and pull request, Ubuntu, Python 3.12,
   `uv sync --frozen --dev`, `uv run pytest`. No lint job (ruff is not a dependency
   today; adding one is a separate decision).
3. **Docs.** README and CLAUDE.md stop stating a test count ("the full suite, all
   pass; LLM calls are mocked"), so the sentence cannot drift again. Roadmap: Phase 4
   status corrected to Complete (impl approved 2026-07-03 per `docs/handoffs/`),
   Phase 6 rewritten to point at the Aegis arc (B1, B2), this phase added as Phase 7.
   Decision log entry for the two seams.
4. **Per-request LLM override (D3).** `POST /reports/{id}/analyze` accepts an optional
   `llm` object with `provider`, `model` and `api_key` (pydantic `SecretStr`), valid
   only with `tier: cloud` (422 with `tier: local`). Missing fields fall back to the
   project's `fix_llm_*` then the globals, exactly as today. The override travels as
   an argument of the in-process background task (`process_fix_proposal` and
   `propose_fix` gain an optional `llm_override`) and reaches `llm_client.complete`
   as `provider`, `model`, `api_key`. It is never written to any table, never logged,
   never returned in any response, and never cached; this holds on the failure path
   and on validation errors, not only on success (see Technical Approach, Secrecy).
   The override also carries an optional non-secret `key_ref`: an opaque reference
   to the Aegis AI settings row that supplied the key (direction doc risk item
   "cloud spend attribution"). `token_usage` gains two nullable columns,
   `key_source` (`request` or `env`) and `key_ref`, so a usage row names the settings
   row that paid for it. `AnalyzeResponse` gains `llm_source` (`request`, `project`,
   `global`).
5. **Per-project ingest source.** `projects` gains nullable `ingest_source` (`supabase`
   is the only accepted value for now) and `ingest_config` (JSON object, stored as
   text). Invariant: both null or both set, enforced after merging a PATCH with the
   stored row and before any write. For `supabase` the config is exactly
   `{url, table, credential_env}`, extra keys forbidden (this replaces a secret-name
   blacklist: there is no field a credential could ride in). `credential_env` is the
   env var *name* holding the credential, never the credential. `url` must be https
   with no userinfo, query or fragment. Carried by `ProjectCreate`, `ProjectUpdate`
   and `ProjectResponse`. B1 builds the poller on top; B0 ships no poller.
6. **Health.** `GET /health` reports `auth_enabled` (false when `BUGALIZER_API_KEYS` is
   empty). The Aegis engine proxy (A1) refuses to start against a Bugalizer that
   reports false.
7. **Windows service check, as a script.** `scripts/windows/check-service.ps1` prints
   the deployed commit, `/health` (including `auth_enabled`), the configured Ollama
   host reachability and which deploy option (Docker or NSSM) is active. One-line
   invocation added to `docs/deploy-windows.md` §6. Greg runs it on BOWIE after merge;
   its output is the post-merge acceptance record.

**Non-goals (later phases)**

- No Supabase poller, no write-back (B1). No apply, commit, push or PR (B2). No
  changes in the Aegis repo (A1). No new role or auth model.
- No change to the local tier or to `analysis_mode` semantics.
- No Ollama model consolidation (7b vs 14b); noted for the BOWIE check only.
- No lint tooling, no dependency upgrades beyond what `uv sync --frozen` already pins.

## Technical Approach

- **Override flow.** `AnalyzeRequest.llm: LLMOverride | None`. `analyze_report`
  validates tier, then `background_tasks.add_task(process_fix_proposal, report_id,
  llm_override=body.llm)`. In `propose_fix`, resolution becomes: request field if
  present, else `resolve_fix_llm(project)`. `complete()` already accepts `api_key` for
  `anthropic` and for the passthrough branch, so OpenAI and other litellm providers
  come along with no client change. `key_source` is `request` when the override
  carried a key, else `env`.
- **Attribution (`key_ref`).** `LLMOverride.key_ref: str | None`, max 128 chars,
  charset `[A-Za-z0-9._:/-]`. Aegis (A1) sends the settings row identity, for example
  `aegis:ai_settings:<row-id>`; Bugalizer treats it as opaque. Rules: `key_ref` without
  `api_key` is a 422 (a reference to a key that was not sent would be a false ledger
  entry); `api_key` without `key_ref` is allowed and records `key_source=request,
  key_ref=null` (headless or curl use); `key_ref` equal to the `api_key` value is a 422
  (closes the obvious footgun). `propose_fix` passes `key_source` and `key_ref` to
  `token_usage_create`; usage rows returned by the `/usage` endpoints carry both
  fields. Test: two mocked cloud runs on the same project with distinct `key_ref`
  values produce two usage rows whose `key_ref` round-trip distinctly through the
  usage API; an env-key run records `env`, `null`.
- **Secrecy, all paths.** The key is a `SecretStr` inside `LLMOverride`; it is
  unwrapped once, in `propose_fix`, into a local passed to `complete()`. Three leak
  paths are closed explicitly:
  1. *Provider exceptions.* Once the plain key reaches the provider, exception text
     can contain it. `fix_proposer` gains `_safe_error_text(exc, secrets)`: builds
     `"<ExceptionType>: <message>"`, replaces every occurrence of each supplied secret
     with `[redacted]`, and as a second net replaces anything matching the common key
     shapes (`sk-...`, `sk-ant-...`, `Bearer <token>`). The `logger.error` line and the
     persisted `analyses.result.error` both use this text; `str(exc)` is no longer
     written raw anywhere on the fix path (the `FixProposalDefer` info line goes
     through the same helper for uniformity, though it fires before any paid call).
     Classification is unchanged: `_classify_llm_error` keys on exception type, and
     its one message check (`API_KEY` in a `RuntimeError`) reads the message without
     persisting it.
  2. *Validation errors.* FastAPI's default 422 body echoes each error's `input`,
     which for a body-level error is the whole request including `api_key`. `main.py`
     registers a `RequestValidationError` handler that drops `input` and `ctx` from
     every error entry (project-wide; nothing else relies on `input`). The tier
     mismatch (override with `tier: local`) and the `key_ref` rules raise
     `HTTPException(422)` with fixed detail strings that never interpolate request
     fields.
  3. *Logging of the request.* No log line on the analyze or fix path formats the
     request body or the override; `repr(LLMOverride)` masks the key as a fallback.
- **Secrecy tests** (`tests/test_fix_proposer.py`, `tests/test_api.py`), each using a
  sentinel key like `sk-ant-SENTINEL-…` and `caplog` at DEBUG on the root logger:
  1. Success: mocked `complete` returns a valid diff. Assert the key is absent from a
     dump of every table, from `caplog.text`, and from `GET /reports/{id}`,
     `/analyses`, `/fix_proposals` and `/queue` bodies.
  2. Provider failure: mocked `complete` raises `AuthenticationError("invalid key
     sk-ant-SENTINEL-…")`. Assert the failed `fix` analysis row exists with
     `permanent=true` and `error` containing `[redacted]`; assert the key is absent
     from every table, `caplog.text`, `GET /reports/{id}` (`last_error`), `/analyses`
     and `/queue`.
  3. Transient failure: mocked `complete` raises `RateLimitError` whose message
     contains the key. Same absence assertions; `permanent=false`.
  4. Validation error: POST analyze with `llm.api_key` set and an invalid `tier`
     value, then with `tier: local` plus `llm`, then with `key_ref` but no `api_key`.
     Each is a 422 whose body does not contain the key.
  5. `repr(LLMOverride)` and `model_dump()` mask the key; `model_dump()` of
     `AnalyzeRequest` never appears in a log line (grep `caplog.text`).
- **Failure path classification.** Unchanged: existing transient/permanent
  classification and the `max_fix_retries` cap apply to request-key runs exactly as
  to env-key runs. What changes is only the error text that is logged and persisted
  (sanitized as above). A1 surfaces the sanitized text.
- **Migrations.** Four `ALTER TABLE ... ADD COLUMN` steps in `db._migrate`
  (`token_usage.key_source`, `token_usage.key_ref`, `projects.ingest_source`,
  `projects.ingest_config`), same shape as `fix_llm_provider`. Test: `_migrate` run
  twice on a database seeded with a pre-existing project row and usage row; both
  runs succeed and the existing rows read back with the new columns null.
- **Ingest config validation and PATCH semantics.**
  - Models: `SupabaseIngestConfig(url, table, credential_env)` with `extra="forbid"`;
    `url` validated as https with empty userinfo, query and fragment; `table` matches
    `^[A-Za-z_][A-Za-z0-9_]*$`; `credential_env` matches `^[A-Z][A-Z0-9_]*$`.
    `validate_ingest_pair(source, config)` is the single checkpoint: `(None, None)`
    passes; `(supabase, dict)` validates the config model; any other combination is a
    422 (`source` without `config`, `config` without `source`, unknown source).
  - Create: `ProjectCreate` carries both fields (default null); the pair is validated
    before insert.
  - PATCH: `update_project` loads the stored row, overlays the supplied fields
    (`exclude_unset`, as today), then runs `validate_ingest_pair` on the merged
    values, then writes. So a config-only PATCH against a project that already has
    `ingest_source=supabase` validates against that stored source and succeeds; a
    config-only PATCH against a project with no source is a 422. Null on either field
    clears both (the write sets both columns null); `ingest_source` and
    `ingest_config` join the endpoint's `nullable` set.
  - Storage: `ingest_config` is JSON text in SQLite; `ProjectResponse` returns the
    parsed object. Never-store-credentials is guaranteed by the schema of the config
    model (three declared fields, no extras, env var name only, credential-free URL),
    not by a key-name list.
  - Tests (`tests/test_api.py` projects section): create with a valid pair (201,
    round-trips through GET); create with source only and config only (422 each);
    config-only PATCH on an existing supabase project (200, source retained);
    config-only PATCH on a project with no source (422); PATCH `ingest_source: null`
    and separately `ingest_config: null` (200, both fields null afterwards); extra
    field such as `service_key` (422); `url` with userinfo, with a query string, and
    with `http` (422 each); lowercase or dashed `credential_env` (422); unknown source
    (422); migration idempotency as above.
- **CI runner.** Ubuntu only. BOWIE does not run the suite (Docker or NSSM deploy), so
  the Windows-portability rule from the Aegis repo does not apply here yet.

## Files

- New: `.github/workflows/ci.yml`, `docs/phases/bugalizer-revive.md`,
  `scripts/windows/check-service.ps1`, `AGENTS.md` (written by `tagteam upgrade`, staged).
- Changed (already staged from the tree cleanup): `CLAUDE.md`, `README.md`,
  `docs/roadmap.md`, `docs/workflows.md`, `docs/checklists/code_review.md`,
  `templates/*.md`, `.claude/skills/review/SKILL.md`, `.gitignore`, `pyproject.toml`,
  `uv.lock`.
- Changed (impl): `README.md` and `CLAUDE.md` (test count), `docs/decision_log.md`,
  `docs/deploy-windows.md` (§6 one-liner), `src/bugalizer/models.py`,
  `src/bugalizer/api/reports.py`, `src/bugalizer/api/projects.py`,
  `src/bugalizer/pipeline/orchestrator.py`, `src/bugalizer/pipeline/fix_proposer.py`
  (`_safe_error_text`, override resolution, attribution), `src/bugalizer/db.py`
  (four migrations, `token_usage_create` fields), `src/bugalizer/main.py` (health,
  validation-error handler).
- Deleted / untracked (already staged): `.claude/skills/handoff/SKILL.md` (deleted),
  `.handoff-session.json` and `.tagteam/.write.lock` (untracked, now ignored).
- Tests: `tests/test_api.py` (override validation incl. 422 secrecy, health, projects
  ingest create/PATCH matrix, validation handler), `tests/test_fix_proposer.py`
  (override reaches `complete`, fallbacks, success/failure/transient secrecy,
  `_safe_error_text`), `tests/test_usage.py` (`key_source`, `key_ref` round trip),
  a migration idempotency test next to the existing `_migrate` coverage.

## Success Criteria

1. CI workflow runs pytest on push and pull request and is green on the phase PR.
2. `git status` clean after the phase commit; `tagteam state` shows the plugin contract
   command, not the deleted vendored skill.
3. README and CLAUDE.md contain no test count; both describe the suite the same way.
4. Roadmap: Phase 4 Complete, Phase 6 points at B1 and B2 with the Aegis direction doc
   path, Phase 7 is this phase. Decision log has the entry.
5. A mocked cloud analyze with a request-supplied provider, model, key and `key_ref`
   calls `complete` with exactly those provider, model and key values; the usage row
   has `key_source=request` and that `key_ref`; the key appears in no table, no log
   line and no API response body.
6. Override fields omitted fall back to project then global values; the usage row has
   `key_source=env`, `key_ref=null`. Override with `tier: local`, `key_ref` without
   `api_key`, and `key_ref` equal to the key are each a 422 whose body does not
   contain the key.
7. A mocked provider failure whose message contains the request key (permanent and
   transient variants) records a failed `fix` row with the existing classification
   and `[redacted]` in the error text; the key is absent from every table, the
   captured log and the report, analyses and queue responses.
8. Project create, PATCH and GET round-trip `ingest_source` and `ingest_config`
   (config-only PATCH succeeds against a stored source; null on either clears both);
   source-only, config-only-without-source, unknown source, extra config fields,
   credential-bearing or non-https URLs, and malformed env var names are 422s.
9. `_migrate` is idempotent on a database with pre-existing project and usage rows.
10. `/health` includes `auth_enabled` and it is false with empty `BUGALIZER_API_KEYS`.
11. `scripts/windows/check-service.ps1` exists, is referenced from `docs/deploy-windows.md`
    §6, and runs on BOWIE (Greg's post-merge acceptance).
12. Full suite green at the submitted commit; no em dashes in any new file or changed line.
