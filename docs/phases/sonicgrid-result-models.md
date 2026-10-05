# Phase 13: sonicgrid-result-models (B5)

**Status:** plan codex-approved 2026-10-04 (round 1); impl codex-approved 2026-10-04 (round 1, 530 tests). Acceptance pending (deploy order below).
**Depends on:** Phase 11 (B3 results push), Phase 12 (B4); sonicgrid Phase 52
`bugalizer-result-models` (PR #639, merged to sonicgrid `main` as `1e48ec1cf`).
**Contract:** sonicgrid `documentation/BUGALIZER-TRIAGE-ENDPOINTS.md`, "Push a result",
row `admin.triageModel`, `admin.localizationModel`, `admin.fixModel`.

## Summary

Sonicgrid's admin drawer now has a "Models" line, but it is empty because Bugalizer doesn't
send model names. This phase adds three optional `admin` fields to the result payload. Each
names the model that actually produced that part of the analysis, written as
`<provider>/<model>`. Admins can then see which model triaged, localized and fixed a bug, and
compare models.

## Scope

In:
- `build_payload` (`src/bugalizer/sync/results.py`) adds `admin.triageModel`,
  `admin.localizationModel` and `admin.fixModel`: a string of 1 to 120 characters, or null.
- Tests: the fake sonicgrid's strict schema check accepts the three optional keys with the
  contract's limits, plus unit tests for each label rule below.
- Docs: CLAUDE.md status line and the roadmap entry.

Out:
- No schema change and no migration (see §2 for the alternative considered).
- No dashboard change. The Bugalizer UI already shows `llm_model` per analysis.
- No change to how models are chosen, and no change to revision or fingerprint logic.

## Technical approach

### 1. Label format: `<provider>/<model>`, no double prefix

`analyses.llm_provider` / `llm_model` record what `llm/client.py` actually sent to litellm
(`LLMResponse.provider` / `.model`). That model string **already carries the provider
prefix** for the two built-in providers: `ollama/qwen2.5-coder:14b` and
`anthropic/claude-sonnet-5-5`. A passthrough provider stores the litellm string verbatim,
which may or may not be prefixed. Joining `provider + "/" + model` naively would push
`ollama/ollama/qwen2.5-coder:14b`. One helper handles all cases:

```python
MODEL_MAX = 120

def _model_label(analysis) -> Optional[str]:
    model = analysis.get("llm_model") if analysis else None
    if not isinstance(model, str) or not model:
        return None
    provider = analysis.get("llm_provider")
    if isinstance(provider, str) and provider and not model.startswith(provider + "/"):
        model = f"{provider}/{model}"
    return model[:MODEL_MAX]
```

Truncating at 120 matches how `results.py` treats every other string limit, where `_text`
truncates instead of failing the push.

### 2. Which analysis each field comes from

| Field | Source |
|---|---|
| `triageModel` | the latest completed `triage` analysis, the same row `summary`/`category` already come from |
| `localizationModel` | the latest completed `localization` analysis, the same row `candidateFiles` / `rootCauseHypothesis` come from |
| `fixModel` | the `fix` analysis that produced the newest proposal (`proposals[0]`, the same proposal `diff`/`explanation` come from); null when there is no proposal |

**Correction to the roadmap entry:** `fix_proposals.analysis_id` is **not** the fix analysis.
`fix_proposer.py` sets it to the *localization* analysis the fix was built on (line 345: the
localization that `latest_completed_localization` returns; line 450: `analysis_id=analysis["id"]`). It
also uses that field to dedup one proposal per localization. Reading the model through
`analysis_id` would label every fix with the local localization model.

Instead, `fixModel` comes from **the newest completed `fix` analysis created no later than
`proposals[0].created_at`**. That pairing is exact:
- `fix_proposer.py:437-457` writes the `fix` analysis and then its proposal, one after the
  other, under `db_write_lock`.
- `_now()` is strictly monotonic, so the analysis's `created_at` always comes before its
  proposal's.
- Only `fix_proposer` creates proposals, and the `FIX_PROPOSING` atomic claim prevents two
  fix runs on one report at the same time.

So no other fix analysis for the same report can land between the two writes. A later fix
attempt that failed with no proposal is excluded because it is either not `completed` or
newer than the proposal. If none matches, `fixModel` is null.

*Alternative considered:* add a nullable `fix_proposals.fix_analysis_id` column through
`_migrate()` and set it in `fix_proposer`. That is more explicit, but it needs a migration,
and existing rows would still need the derivation above as a fallback. That's two mechanisms
where one is enough. Rejected unless the reviewer sees a pairing case this misses.

### 3. Fingerprint and revision: one re-push per bug, then quiet

`fingerprint()` hashes the whole payload, `admin` included, so after deploy every
sonicgrid-sourced report's fingerprint changes once and `_push` re-sends it. The revision is
`max(last + 1, epoch_us)` (`triage_sync.py:723-724`), which is strictly above the stored one,
so sonicgrid applies the re-push (`applied: true`) rather than acknowledging it as stale.
`triage_push_per_tick` spreads that backlog over ticks (existing
`test_push_budget_spreads_a_backlog_over_ticks`). After that, a model field changes only when
a new analysis completes, and those runs already re-push.

## Rollout (deploy order)

Sonicgrid must accept the fields **before** BOWIE sends them. Sonicgrid's object schemas are
strict, so an older sonicgrid answers `400` to every push. `_push` doesn't retry a `400` for
the same fingerprint (`test_client_errors_are_not_retried_for_the_same_fingerprint`), so the
board would freeze until each bug changed again.

1. Sonicgrid #639 deployed to **production**. Merged on `main` is the code, not the deploy;
   Jack confirms the Vercel production deploy.
2. Merge this phase, then redeploy BOWIE (`lan-mgr-bugalizer`) and check `/health` `revision`.
3. Acceptance (below).

No feature flag. The deploy order above is the whole gate, and a flag would outlive its one
use.

## Files (expected)

- `src/bugalizer/sync/results.py`: `MODEL_MAX`, `_model_label`, a `_fix_analysis` pairing
  helper, and three `admin` keys in `build_payload`.
- `tests/test_triage_sync.py`: `ADMIN_OPTIONAL` in the fake's schema check (1-120 chars or
  null), unit tests, and one end-to-end push assertion.
- `CLAUDE.md`, `docs/roadmap.md`: status, plus the corrected `fixModel` source.

## Testing

Unit tests (pure `build_payload`):
- an already-prefixed `ollama/qwen2.5-coder:14b` stays as is, and an unprefixed `anthropic` + `claude-x` becomes
  `anthropic/claude-x` (no double prefix);
- a passthrough `openai` + `gpt-4o` becomes `openai/gpt-4o`, and `openai/gpt-4o` stays unchanged;
- a null or empty model gives null, and a null provider gives the bare model;
- a label over 120 characters is truncated to 120, and the payload still passes `_schema_error`;
- with no triage, localization or proposal, all three fields are null;
- fix pairing: two proposals, each built on a different-model fix analysis, give `fixModel`
  = the newest proposal's model; a newer *failed* fix analysis and a newer completed fix
  analysis with no proposal are both ignored; `fixModel` never equals the localization model
  that `analysis_id` points to.

Integration (fake sonicgrid):
- a pushed result carries the three fields and passes the strict check;
- a report pushed with no model fields (the old fingerprint) re-pushes once with a higher
  revision and is applied, and a second tick sends nothing.

Full suite once, on the record, at submission.

## Success criteria

- Every push carries the three keys, with values that are the contract's string-or-null.
- No label has a doubled provider prefix.
- `fixModel` names the fix model (for example `anthropic/…` on a cloud fix), never the
  localization model.
- Existing reports re-push once and are applied; no `400`s.
- Acceptance on BOWIE: a triaged bug in sonicgrid `/admin/bugs` shows "Models: triage
  ollama/gemma4:12b · localization ollama/qwen2.5-coder:14b"; a cloud-fixed bug also shows
  `fix anthropic/…`. Don't hand-push a result to production to test this, because a high
  revision would block later real pushes.

## Risks and open points

- **Deploy order** (above) is the one real risk, and it's operational, not code.
- `llm_model` on old analysis rows written before the provider normalization could be
  unprefixed. The label rule adds the prefix, so those rows show correctly too.
