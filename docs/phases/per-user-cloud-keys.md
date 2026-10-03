# Phase 12: per-user-cloud-keys (B4), with the sonicgrid bug board

**Status:** plan, round 3 (2026-10-03). Cross-repo: this document is the plan for both sides;
each repo builds its part in its own cycle (Bugalizer here as Phase 12 / B4; sonicgrid as S3
`sonicgrid-ai-settings` and S4 `sonicgrid-bug-board` in sonicgrid's tagteam).
**Depends on:** Phase 11 (sonicgrid-triage-sync), Phase 8 (open-pr; its GitHub token).

## Summary

Sonicgrid admins can't follow a bug through Bugalizer today: `/admin/bugs` shows three tabs
(Active / Resolved / "Bugalizer triage") and the triage tab stacks one card per raw pipeline
status (up to 14), next to a separate sonicgrid active/resolved flag. Cloud work runs on the
BOWIE owner's Anthropic key behind an allowlist (`BUGALIZER_SONICGRID_CLOUD_USERS`).

This phase:

1. replaces `/admin/bugs` with one six-lane board (Submitted, In triage, Triaged, In fix,
   In review, Completed) and one status, with basic styling taken from Bugalizer's dashboard;
2. lets each admin store their own Claude API key and default model in sonicgrid; Bugalizer
   uses the requester's key for that one call and never stores it; the allowlist is retired;
3. lets Bugalizer see a fix PR's fate on GitHub, so a merged PR completes the bug.

## Decisions (Jack, 2026-10-03)

| # | Decision |
|---|---|
| F1 | Admins only (sonicgrid `ADMIN_EMAILS`) see the board, act, and hold keys. A dedicated bug-admin role may replace `ADMIN_EMAILS` later without changing this design. Reporters keep `/bugs`. |
| F2 | Claude (Anthropic) only for now. Supersedes the "Anthropic or Codex/OpenAI" wording of E4 for this phase; `provider` stays a stored field so another provider can be added later. |
| F3 | Each admin's own key and default model, on an Aegis-style AI settings page in sonicgrid. Nobody spends another person's key. Having a saved key replaces the cloud allowlist. |
| F4 | Key transport: sonicgrid stores the key encrypted; Bugalizer fetches it once per claimed paid action through a new credential endpoint and holds it only in memory for the call. (Rejected: encrypting to a Bugalizer public key, more key management; storing keys in Bugalizer, Vercel cannot reach BOWIE and it reverses the env-only-secrets decision.) |
| F5 | Completion: Bugalizer reads the PR's state from GitHub. Merged moves the bug to Completed; closed without merge moves it back to Triaged with a note; other endings (won't fix, duplicate, not a bug) are closed by an admin. |
| F6 | One board, one status: the board is the whole `/admin/bugs` page; sonicgrid's `active/resolved` follows the board. The Active/Resolved tabs go. |
| F7 | Basic styling now (colours, borders, lane identity), refined later. |
| — | Dropped: re-separating local and cloud runs (an earlier testing concern). Analyze (local) is unchanged. |

**Invariants** (every section below must keep them):

- **I1. No env-key fallback.** A sonicgrid paid action never reaches a model without the
  requester's own key. Any missing, malformed or mismatched pin or credential fails closed
  before the call.
- **I2. Bugalizer is the status authority.** Sonicgrid's bug status changes only when a
  newer Bugalizer result is accepted (revision-gated), never when an action is queued.
- **I3. No paid call twice for one action.** Phase 11's `no_auto_retry` and
  unconfirmed-failure rules stand; a credential is released at most once per action.

## Scope

**In:** the lane model and stage fields; the board and drawer; basic styling; the AI settings
page; key encryption and the credential endpoint; requester-model pinning; retiring the
allowlist behind a feature gate; PR state polling and the merge/close transitions; reopen;
status sync in the result upsert; contract doc updates; tests on both sides; a BOWIE
acceptance walk.

**Out:** drag and drop between lanes; light theme and custom fonts; providers other than
Anthropic; budget caps or spend dashboards; a list/table view toggle; a second PR for a
report whose PR was closed unmerged (Phase 8's one-PR-per-report rule still applies; the
open-pr refusal names the old PR); the known gap that a cut-off paid call records no
`token_usage` row.

## Technical approach

### 1. Lane model (Bugalizer owns the mapping)

Bugalizer computes the lane and pushes it, so sonicgrid never re-derives pipeline logic. A
pure function `stage_of(report, analyses, proposals, project) -> (stage, stage_detail)` in a
new `sync/stage.py` holds the mapping; `sync/results.py` adds `stage`, `stageDetail` and
`prState` to the public result. `pipelineStatus` stays for compatibility.

| `stage` | Bugalizer status | `stageDetail` |
|---|---|---|
| `submitted` | `submitted`, `validating` (sonicgrid also puts never-imported bugs here) | `queued`, `validating` |
| `triage` | `analyzing` | the phase of the newest `running` analysis row (`triaging`, `localizing`); `starting` when the claim exists but no row yet |
| `triaged` | `triaged`, `clarification_needed`, `deferred` | first match in the priority list below |
| `fix` | `fix_proposing` | `proposing` |
| `review` | `fix_proposed` (no PR), `fix_approved` (open-pr running), `fix_committed` (PR recorded) | `fix_ready`, `opening_pr`, `pr_open` |
| `completed` | `closed`, `rejected`, `duplicate`, `verified` | `merged` (`resolution_reason` starts `pr_merged:`), `rejected`, `duplicate`, `closed` (any other) |

Phase 8 already finalizes `fix_approved → fix_committed` when a PR is recorded
(`db._record_pr_report`, `db.record_pull_request`), so `fix_committed` means "PR open" and
belongs to Review, not Completed.

**Triaged priority** (actionable states first; the card shows the first match, the drawer
shows every fact):

1. `needs_clarification`: status `clarification_needed`.
2. `deferred`: status `deferred`.
3. `held`: `analysis_mode = hold`.
4. `fix_failed`: the newest `fix` analysis row is `failed` (including interrupted), it is
   newer than the newest completed localization, and no fix proposal is newer than it. A
   newer successful proposal or a newer localization clears it.
5. `pr_closed`: the newest proposal's PR closed unmerged (§2) and nothing newer exists.
6. `stale`: the newest completed localization's `repo_sha` ≠ the project's `head_sha`.
7. `localized`: a fresh completed localization.
8. `not_localized`: none of the above.

### 2. PR state (Bugalizer)

**Storage** (new columns on `fix_proposals`, added in `db._migrate`): `pr_state`
(`open | merged | closed`, null until first read), `pr_checked_at`, `pr_settled_at` (set once,
when a terminal state has been applied or deliberately skipped).

**Polling:** a step in the triage sync loop, only when `BUGALIZER_GITHUB_TOKEN` is set,
selects proposals with `pr_number` set and `pr_settled_at IS NULL` whose report is
`fix_committed`, at most once per 5 minutes per PR (`pr_checked_at`). It reads
`GET /repos/{owner}/{repo}/pulls/{n}` with the Phase 8 client and token. The owning report
is the proposal's `bug_report_id`. 404/401/rate limit leave everything unchanged, update
`pr_checked_at`, and count on `/health` `triage_sync`; never retried hot.

**Terminal transition**, one transaction per PR:

- `merged`: `UPDATE bug_reports SET status='closed', resolution_reason='pr_merged:<n>' WHERE
  id=? AND status='fix_committed'`; set the proposal's `pr_state='merged'` and
  `pr_settled_at`.
- `closed` unmerged: the same CAS to `triaged`; `pr_state='closed'`, `pr_settled_at`. The
  note is the `pr_closed` stage detail plus a timeline entry built from the proposal.
- If the CAS matches nothing (an admin changed the status meanwhile), the PR state is still
  recorded and `pr_settled_at` set; the status is left alone.
- `open`: update `pr_state` and `pr_checked_at` only.

These are internal transitions (not PATCH /status), like the rest of the pipeline's
`report_update_status` moves; `validate_transition` is not on this path. **Restart:** the
selection is durable (`pr_settled_at IS NULL`), so a crash between read and write re-reads
the PR next tick; the CAS makes a repeat harmless.

**Reopen after merge** never re-closes: the merged proposal has `pr_settled_at` set and is
never polled again.

### 3. Reopen

A new action kind `reopen` (admin, free, inline like `close`). It is refused for `rejected`
and `duplicate` (they stay Completed), and for any status other than `closed`, with the
current status in the message.

**Atomic with its outcome.** Phase 11 replays a `dispatched` inline action after a restart,
and a plain CAS cannot tell "this action already reopened it" from "it was never closed".
So the transition and the action's local outcome are one SQLite transaction, in a new
`db.reopen_for_action(report_id, action_id)`:

1. `UPDATE bug_reports SET status='triaged', resolution_reason=NULL, updated_at=? WHERE
   id=? AND status='closed'` (clearing the old reason, so a later manual close is not
   labelled `merged`);
2. only if that matched one row: the same ledger write `triage_action_finish` performs
   (outcome `done`, message, phase finished) with `expected_phase='dispatched'`;
3. commit. If step 1 matched nothing, nothing is written and the caller finishes the action
   `refused` through the normal `_finish`.

After a crash, a reopen whose transaction committed is already finished in the ledger, so
recovery only posts the stored outcome (terminal drain) and never re-runs it; one that did
not commit changed nothing and is replayed against the real status. A worker advancing the
report after the commit therefore cannot turn a successful reopen into a refusal. The result push
that follows (new revision, `stage=triaged`) is what moves sonicgrid's status back (I2). An
unknown kind is already refused by `actions.authorize` (`KINDS` check), so a reopen queued
before B4 is deployed fails visibly and changes nothing.

### 4. Keys, credential and dispatch

**Sonicgrid storage and settings (S3b):**

- **Settings page** (`/admin/ai-settings`, admins only): provider (fixed: Anthropic), key,
  default model. On save, sonicgrid calls Anthropic's `GET /v1/models` with the key; a
  failure rejects the save with the reason, success fills the model dropdown with what that
  key can use. After saving, the page shows only `sk-ant-…<last4>`, with Replace and Delete.
- **Table** `ai_settings`: `user_id` PK, `provider`, `model`, `key_ciphertext`, `key_iv`,
  `key_last4`, `key_version`, `updated_at`; RLS denies all client roles (service role only).
  AES-256-GCM, a fresh IV per write, AAD = `user_id`, the master key in a Vercel env var
  (`AI_SETTINGS_ENCRYPTION_KEY`), `key_version` for rotation. The key is never returned to
  the browser.
- **Queueing a paid action** (`analyze_cloud`, `fix_and_open_pr`): refused in sonicgrid with
  "Add your Claude API key in AI settings" when the requester has no key. Otherwise
  `params.llm = {provider: "anthropic", model: <requester's default>}` is written into the
  action (pinned at queue time). `requested_by_user_id` already identifies the requester and
  is immutable. The `kind` CHECK constraint on `bugalizer_actions` gains `reopen` (S3a).

**Credential endpoint (S3b):** `POST /api/bugalizer/actions/{id}/credential`, triage-token
auth, no request body. One SQL statement both validates and marks the release:

```sql
UPDATE bugalizer_actions SET credential_released_at = now()
WHERE id = $1 AND state = 'claimed' AND kind IN ('analyze_cloud','fix_and_open_pr')
  AND credential_released_at IS NULL
RETURNING requested_by_user_id, requested_by_email, params
```

Only the caller that gets the row decrypts the key of **that row's `requested_by_user_id`**
(never from input). Responses: 200 `{provider, apiKey}` with `Cache-Control: no-store`; 409
`already_released`, `not_claimed` or `not_paid`; 404 `not_found`; 403 `requester_not_admin`
(removed from `ADMIN_EMAILS` since queueing); 409 `no_key` (key deleted since queueing). A
replaced key releases the current key; the model stays the pinned one. Bodies are never
logged and are excluded from error reporting.

**Bugalizer authorization** (`sync/actions.authorize`, gate on, §Rollout): for
`analyze_cloud` and the fix step of `fix_and_open_pr`, the pin comes only from
`params.llm`. Absent or invalid params, or `provider != "anthropic"`, → `refused`
("requested without a model pin; request again"). The allowlist is not consulted.
`no_auto_retry` stays as today. The ledger records `key_mode = 'requester'` for these.

**Dispatch order** (inside `_run_task`, after `_dispatch` has durably recorded
`dispatched`), for a `key_mode='requester'` action:

1. Call the credential endpoint once.
2. Validate: HTTP 200, JSON object, `provider == "anthropic" == pinned provider`, `apiKey` a
   non-empty string. Anything else ends the action without a model call:

   | Outcome | Action result |
   |---|---|
   | 200 valid | continue |
   | 200 malformed / provider mismatch | `failed`: "invalid credential response" |
   | 409 `already_released` | `failed`: "credential already used; request again" |
   | 409 `no_key` / 403 / 409 `not_claimed`, `not_paid` / 404 | `refused` with the reason |
   | timeout, connection error, 5xx | `failed`: "could not fetch your API key; request again" |

   A lost response is **not** retried: a release may have happened, and I3 allows one per
   action. The admin requests again (a new action id, a new release).
3. Call `propose_fix(..., llm_override=LLMOverride(provider, model, api_key,
   key_ref="sonicgrid:<user id>"), require_request_key=True)`. The new
   `require_request_key` flag makes `propose_fix` raise before any model call when the
   override has no key, so `complete()` can never fall back to the env key (I1). Today a
   keyless override does fall back (`fix_proposer.propose_fix`, `request_key=None`).
4. Compound `fix_and_open_pr`: every credential outcome above maps to the fix step's state
   (`fix: failed/refused`), and the PR step does not run, as today.

**Crash boundaries:** before step 1, or between steps 1 and 3: the ledger says `dispatched`
with no evidence; recovery gives the paid action an unconfirmed failure without
re-dispatch (Phase 11 `_recover`), so no second fetch. During step 3: as today
(unconfirmed, or adopted evidence). The startup release (PR #12/#13) is unchanged.

### 5. Single status (sonicgrid S3a, I2)

`upsert_bugalizer_result` (sonicgrid migration) also updates `bug_reports.status` in the same
statement, and only when the push is applied (newer revision): `stage = 'completed'` →
`resolved`; any other stage → `active`; a push without `stage` (older Bugalizer) leaves the
status alone. A delayed or retried older push is rejected by the existing revision gate, so
it cannot undo a reopen. Queueing `reopen` changes nothing in sonicgrid; the card shows
"reopen queued" from the open action until the new result arrives.

**Legacy and never-imported bugs:** a bug with no Bugalizer result sits in Submitted with
its current status; its drawer keeps sonicgrid's existing Resolve/Reopen (no Bugalizer
copy to act on). A result without `stage` is placed by a static `pipelineStatus → lane`
fallback in sonicgrid mirroring §1 (no details).

### 6. The board (sonicgrid S4)

- `/admin/bugs` becomes the board: six lanes left to right, a card per bug, a side drawer on
  click (the board keeps its scroll position). Lanes stack on narrow screens.
- **Card:** title (first line of the report), severity, age, a "working" marker (`triage`,
  `fix`, `opening_pr`, or an open action), the `stageDetail` chip, PR number and state in
  Review, the ending in Completed.
- **Drawer:** the report; the latest result (summary, root cause, candidate files,
  colourised diff); one primary action for the lane (Submitted: Analyze (local); Triaged:
  Propose fix (Claude); Review: Open PR or View PR; Completed with `merged`/`closed`: Reopen;
  none for `rejected`/`duplicate`) and a "More" menu; analysis mode as one select; a timeline
  of actions, steps and results with who asked.
- **Behaviour:** poll every 15 s while the tab is visible, plus Refresh; Completed collapsed
  to the last 14 days; each empty lane says what would put a bug there.
- **Reporter view** (`/bugs`): the same six stage names, public fields only.

### 7. Styling (sonicgrid S4, basic)

Bugalizer's Slate palette (`src/bugalizer/static/styles.css`) as Tailwind tokens scoped to
the bug pages: slate background `#2b3242`, cards `#333b4e` with a 1px `#465067` border and
a hover lift; each lane a coloured top border and tinted header with a count (steel
Submitted, amber In triage, blue Triaged, violet In fix, teal In review, green Completed);
severity as a card left edge plus a labelled chip (critical `#ef6b6b`, high `#ee9a55`, medium
`#e8c65a`, low `#7d93bd`); emerald for local/free and violet for Claude/paid on action
buttons and timeline entries; muted secondary text; a dark code panel for diffs. Colour is
never the only signal, and text meets WCAG AA contrast.

## Contract changes

Sonicgrid's `documentation/BUGALIZER-TRIAGE-ENDPOINTS.md` is the contract (as in S2/B3); its
result schema is strict, so sonicgrid lands the contract first:

- public result: optional `stage`, `stageDetail`, `prState`;
- action kinds: `reopen`;
- paid actions: `params.llm = {provider, model}`;
- the credential endpoint and its responses (§4);
- `upsert_bugalizer_result` status sync (§5).

## Rollout (mixed versions)

| Step | Ships | Behaviour while the other side is older |
|---|---|---|
| 1. S3a (sonicgrid) | schema accepts the new optional fields; `reopen` kind; upsert status sync; static lane fallback | Old Bugalizer pushes no `stage`: status untouched, lanes from the fallback. |
| 2. B4 (Bugalizer), gate **off** | stage fields, PR polling, reopen, `require_request_key`, the requester-key path behind `BUGALIZER_SONICGRID_USER_KEYS=false` | Legacy paid actions (no `params.llm`) keep today's rules (allowlist, project fix resolution, env key). **A paid action that carries `params.llm` is refused** ("per-user keys are not enabled on Bugalizer yet") and never runs on the old path. The credential endpoint is never called. |
| 3. S3b (sonicgrid), queueing flag **off** | settings page, storage, credential endpoint; own-key paid queueing behind a sonicgrid flag `BUGALIZER_USER_KEYS_ENABLED=false` (Vercel) | Admins can save and test keys. Paid actions are still queued in the legacy format (no `params.llm`) and run under the allowlist as before. |
| 4. Activate (coordinated) | see the activation sequence below | — |
| 5. S4 (sonicgrid) | board, drawer, styling | Can ship any time after step 1; Reopen works once step 2 is live (refused before, via the existing `KINDS` check). |

**Activation sequence** (one session, Jack):

1. In sonicgrid, stop new paid queueing: the cloud buttons are hidden while
   `BUGALIZER_USER_KEYS_ENABLED=paused` (a third flag value; free actions still queue).
2. Quiesce BOWIE: wait until `/health` `triage_sync` shows no held or dispatched paid
   actions (or their 45-minute bound has passed), then stop `lan-mgr-bugalizer`. Nothing
   old-mode is in flight from here on.
3. Set `BUGALIZER_SONICGRID_USER_KEYS=true`, remove `BUGALIZER_SONICGRID_CLOUD_USERS`,
   start the service.
4. Set sonicgrid's flag to `true`: paid actions now carry `params.llm` and need a saved key.

**Drain rules** (Bugalizer, gate on): a pending paid action without `params.llm` is refused
("requested before per-user keys; request again"); a ledger action authorized under the old
rules (`key_mode` not `requester`) that has not dispatched is refused the same way; a
dispatched one follows Phase 11 recovery (unconfirmed, no call). With the gate off, an
action carrying `params.llm` is refused (step 2 row). So an action is never charged to a
key other than its contract says: own-key actions never run on the env key, at any point
in the rollout (I1). The allowlist code is deleted in a follow-up once the gate has been on
through one acceptance walk.

## Files (expected)

Bugalizer: new `sync/stage.py`; `sync/results.py` (stage, stageDetail, prState);
`sync/actions.py` (params pin, `reopen`, gate, drain refusals, credential outcomes);
`sync/triage_sync.py` (PR state step, credential fetch in `_run_task`, ledger `key_mode`);
`pipeline/fix_proposer.py` (`require_request_key`); `git_ops/pull_request.py` (PR read);
`db.py` (`fix_proposals` PR columns, settle transaction, `reopen_for_action`, ledger `key_mode`); `config.py`
(`sonicgrid_user_keys`); `.env.example`, `README.md`, `docs/deploy-windows.md`,
`docs/sonicgrid-triage-acceptance.md`; tests in `tests/test_triage_sync.py`,
`tests/test_open_pr.py`, `tests/test_fix_proposer.py`, new `tests/test_stage.py`.

Sonicgrid: `documentation/BUGALIZER-TRIAGE-ENDPOINTS.md`; `src/lib/bugalizer/triage-contract.ts`;
migrations for `ai_settings`, `bugalizer_actions.credential_released_at`, and the new
`upsert_bugalizer_result` (status sync); `src/app/api/bugalizer/results/[bugId]/route.ts`;
`src/app/admin/ai-settings/`; `src/app/api/bugalizer/actions/[actionId]/credential/route.ts`;
`src/lib/actions/bugalizer-triage-actions.ts` (queueing flag `BUGALIZER_USER_KEYS_ENABLED`); `src/app/admin/bugs/page.tsx`;
`src/components/admin/bugalizer/*` (board, card, drawer); `src/components/bugalizer/*`
(badges, reporter view); `src/lib/bugalizer/__sql_tests__/` (upsert and release SQL).

## Testing

- **Stage mapping:** every status; each triaged predicate alone; overlaps (held + localized →
  held; failed fix + localized → fix_failed; failed fix + stale → fix_failed; a newer
  proposal or newer localization clears fix_failed; pr_closed + localized → pr_closed);
  `analyzing` with no running row → `starting`; `fix_committed` → review/`pr_open`.
- **PR lifecycle, end to end:** open a PR through the real Phase 8 finalization
  (`record_pull_request` against the existing git http-backend + mock GitHub), observe
  `review/pr_open`; then a mocked `merged` → `completed/merged` and `pr_settled_at` set;
  separately `closed` → `triaged/pr_closed`. Also: CAS miss after a manual status change
  (state recorded, status kept); restart between read and write (re-read, one transition);
  reopen after merge is not re-closed; 404/401/rate limit → unchanged and counted; no
  polling without the token.
- **Credential and dispatch (fake sonicgrid):** valid release → one call on the requester's
  key, attributed `key_ref=sonicgrid:<id>`, `key_source=request`; concurrent release
  requests → exactly one key (SQL test in sonicgrid); lost response → `failed`, no retry, no
  call; malformed body / provider mismatch / empty key → `failed`, no call, and the env key
  is never used (assert `complete()` not awaited even with `BUGALIZER_ANTHROPIC_API_KEY`
  set); 409 `already_released`; 403/`no_key` → refused; crash after `dispatched` before
  fetch, and after fetch before the call → unconfirmed, no second fetch; compound action →
  fix step state set, PR step not run.
- **Key secrecy:** the key appears in no Bugalizer table, log line, error text, `/health` or
  response (extend `_assert_key_nowhere`); sonicgrid never logs or returns it.
- **Encryption (sonicgrid):** round trip; ciphertext moved to another `user_id` fails (AAD);
  version mismatch fails cleanly; the browser never receives the key.
- **Status sync (sonicgrid SQL tests):** applied completed push → resolved; applied
  non-completed → active; push without `stage` → unchanged; stale completed push after a
  reopen → rejected, status stays active; failed or refused reopen → status unchanged;
  rejected/duplicate offer no Reopen.
- **Rollout compatibility:** old payload without stage fields accepted and placed by the
  fallback; gate off + legacy paid action from an allowlisted requester → runs as today;
  **gate off + `params.llm` from an allowlisted requester → refused, zero `complete()`
  calls, zero env-key spend, endpoint never called**; gate on → legacy paid action without
  params refused, old-mode ledger action refused, env key not used; missing credential route
  (404) → refused, no call; sonicgrid flag `false`/`paused`/`true` → legacy format / no
  paid buttons / `params.llm` with a key required.
- **Reopen:** a fresh reopen of a `closed` report → `triaged`, `resolution_reason` cleared,
  outcome `done`; a fresh reopen of `triaged`, `rejected` or `duplicate` → `refused`, report
  unchanged; crash after the transaction commits but before the post → recovery posts `done`
  without re-running (simulate by committing then resetting runtime state); replay after the
  worker has advanced the report past `triaged` → still `done`; crash before the commit →
  replayed against the real status.
- **Board:** lane placement from fixtures, empty-lane copy, Completed window, Reopen only
  where allowed, a Playwright smoke.
- **Acceptance on BOWIE** (new section in `docs/sonicgrid-triage-acceptance.md`): an admin
  saves a key; Analyze (cloud) runs on that key and is attributed to them; an admin without a
  key is refused at queue time; a PR merged on GitHub moves the card to Completed and
  resolves the sonicgrid bug; a PR closed unmerged returns it to Triaged; Reopen on a merged
  bug returns it to Triaged and active, and it is not re-closed.

## Success criteria

- An admin can tell, from the board alone, which bugs are waiting, which a model is working
  on, which need a person, which have a fix or PR to review, and which are done.
- No sonicgrid action spends a key other than its requester's, and none spends the env key
  once the gate is on (I1); no Bugalizer table, log or response contains a user key.
- A merged fix PR completes its bug without anyone touching the board; sonicgrid's status
  only ever follows accepted Bugalizer results (I2).
- The page uses the agreed lanes, colours and borders, and passes AA contrast.

## Risks and open points

- **Anthropic terms:** secondary sources say end users must authenticate with their own
  credentials and an app may not pay for their usage; that matches F3, but the actual
  commercial terms have not been read. A release prerequisite for S3b.
- **Triage token blast radius:** it can now fetch keys for claimed paid actions. Limited by
  the one-statement release, claimed-and-paid only, the audit timestamp and the admin check
  at release; rotate it if leaked.
- **A closed-unmerged PR blocks a second PR** for the same report (Phase 8 rule); out of
  scope, recorded as a follow-up.
