# Phase 8: open-pr (B2)

## Summary

B2 of the Aegis Bugalizer arc (direction record in the Aegis repo,
`docs/bugalizer-integration-direction-2026-09-12.html`, rulings D1 to D6 by Greg on
2026-09-12; D4 = "Open PR" is branch-only, a human merges on GitHub). Today a
`fix_proposed` report carries a unified diff in `fix_proposals.diff` and Bugalizer
stops there: git access is clone and pull only (`git_ops/repo.py`). B2 adds one
endpoint that turns a proposal into a pull request on the project's GitHub repo,
and unlocks the `fix_approved` and `fix_committed` states for that path only.

This endpoint is the only code in Bugalizer that writes to a remote. The Aegis
"Open PR" button that calls it is A2 (Aegis repo), not this phase.

Depends on Phase 7 (B0) only; B1 is not required, because a report created through
the existing API or dashboard has the same shape as an ingested one.

Arbiter rulings taken for this plan (Greg, 2026-09-17, "your recommendations"):
R1 credential = one fine-grained GitHub token in the service env; R2 a diff that no
longer applies fails with a clear status and never triggers re-analysis; R3 the
branch is built in a throwaway git worktree, never in the analysis clone.

**Revision r2** (codex plan r1): claims carry an owner token so a claim left by a
dead process is adopted, not stuck; remote PR reconciliation runs before the diff is
applied; commits are detached (no local branch ever exists); every remote command
targets the canonical HTTPS URL with the scoped credential, never `origin`;
recorded-PR lookup and proposal ownership are report-wide and checked before any
mutation.

**Revision r3** (codex plan r2): a PR found during reconciliation is attributed to
the proposal that actually opened it (durable push intent, then the commit's
`Bugalizer-Proposal` trailer), never to the request's default selection; an
unattributable PR is refused unless the caller names the proposal; the base fetch
uses `--no-write-fetch-head`.

## Scope

**In scope**

1. **Endpoint.** `POST /reports/{id}/open-pr`, API key required (existing
   `require_api_key`). Optional body `{fix_proposal_id}`. Checks run in this order,
   all before any mutation:
   - `404` unknown report.
   - `404 {code: "proposal_not_found"}` when `fix_proposal_id` is supplied and is
     not a proposal *of this report*. A missing ID and another report's ID get the
     same response, so the endpoint does not reveal which proposals exist.
   - **Report-wide recorded PR.** If any proposal of the report has `pr_url`:
     - no body, or a body naming that proposal: `200 {pr_url, pr_number, branch,
       fix_proposal_id, created: false}` (idempotent repeat);
     - a body naming a *different* proposal: `409 {code: "pr_exists", pr_url,
       pr_number, fix_proposal_id}`, where `fix_proposal_id` is the proposal that
       opened the PR. There is one PR per report, and a second proposal never
       replaces it.
   - Otherwise the proposal is the one named in the body, or else the report's
     newest proposal. `409 {code: "no_proposal"}` if the report has none.
   - `409 {code: "wrong_status"}` unless the report is `fix_proposed` or holds an
     abandoned `fix_approved` claim (see 3). `409 {code: "in_progress"}` when
     another request in this process holds the claim.
   - `503 {code: "github_not_configured"}` when the token is unset. `400` when the
     project repo is not a `github.com` repo or is not cloned.

   Outcomes after the claim:
   - `201 {pr_url, pr_number, branch, fix_proposal_id, created: true}` if a PR was
     opened.
   - `200 {…, created: false}` if reconciliation found this report's PR on GitHub
     (step 5 below). The response's `fix_proposal_id` is the proposal that owns the
     PR. If the caller named a different proposal, the PR is still recorded
     against its owner and the response is `409 pr_exists` naming the owner, the
     same answer a later call gets from the DB.
   - `409 {code: "pr_unattributed", pr_url, pr_number}` if reconciliation finds a
     PR whose owning proposal cannot be established (step 5) and the caller named
     no proposal. Nothing is recorded and the report returns to `fix_proposed`.
   - `409 {code: "diff_does_not_apply"}` if `git apply --check` fails on the
     fetched tip of the default branch. No remote write happens, and the report
     returns to `fix_proposed`.
   - `409 {code: "branch_exists"}` if `fix/bugalizer-<report-id>` exists on the
     remote with no PR and its tip is not a commit this service recorded pushing for
     the report. The branch is never overwritten.
   - `502 {code: "github_error"}` for GitHub API or remote git failures. The token
     and upstream bodies are never echoed.
2. **Write policy (tested, not just documented).**
   - Branch name is always `fix/bugalizer-<report-id>`, computed server-side; the
     caller cannot supply one. A guard refuses any push whose target ref equals the
     project's `default_branch` or lacks the `fix/bugalizer-` prefix.
   - Never force: no `--force`, `--force-with-lease` or `+refspec` on any fetch,
     push or ref update; a test asserts the argv of every git call made by the
     module. (The one `--force` is `git worktree remove --force`, which deletes a
     local scratch directory; the test allowlists exactly that argv.)
   - No local branch is ever created. The commit is built detached in the worktree
     and pushed as `HEAD:refs/heads/fix/bugalizer-<id>`, so no ref is left in the
     shared repository to conflict with a retry.
   - One branch and one PR per report: the PR is looked up (DB report-wide, then
     GitHub by head ref) before any apply, commit or push.
   - Never merge: the module has no merge call, and the PR body says a human merges.
3. **State machine and claim.** On success the report moves `fix_proposed` ->
   `fix_approved` (the call is the human approval; A2 puts a confirmation in front
   of it) -> `fix_committed` (PR open). `fix_approved` is the claim. It is taken by
   compare-and-set, extending `try_claim_report` so that it also writes a new
   nullable `bug_reports.claim_token` (a fresh UUID per request). On any handled
   failure after the claim, the report returns to `fix_proposed` and the token is
   cleared (new transition `fix_approved -> fix_proposed`, a CAS on the request's
   own token).
   - **Abandoned claims.** Bugalizer runs as a single process by design
     (`architecture.md`: single-node, no horizontal scaling). The endpoint keeps an
     in-process registry `{report_id: claim_token}` for requests currently running.
     A report at `fix_approved` whose `claim_token` is not in the registry has lost
     its owner: the process died, or a rollback write itself failed. The next
     request adopts the claim with
     `UPDATE … SET claim_token = :new WHERE id = ? AND status = 'fix_approved' AND claim_token = :observed`.
     If two requests race for an adoption, only one wins; the loser gets
     `in_progress`. A token that *is* in the registry gets `409 in_progress`. The
     single-process assumption goes into the decision log; running uvicorn with
     `--workers > 1` is already unsupported because of the queue worker.
   - Both states stay out of `CURRENT_PHASE_TARGETS`, so
     `PATCH /reports/{id}/status` still cannot enter them by hand; the endpoint
     transitions with `enforce_phase_gating=False`.
4. **Schema.** `fix_proposals` gains nullable `pr_url`, `pr_number`, `pushed_sha`
   and `pr_opened_at`; `bug_reports` gains nullable `claim_token`. Both use the
   `_migrate` ALTER pattern in `db.py`. The existing `branch_name` column is filled,
   and proposal `status` becomes `pr_opened`. `GET /reports/{id}/fix_proposals`
   returns the new proposal fields; `claim_token` is never exposed.
5. **Operator surface.** `.env.example` and `docs/deploy-windows.md` document
   `BUGALIZER_GITHUB_TOKEN` (fine-grained, repository access `spherop/sonicgrid`
   only, permissions Contents read/write and Pull requests read/write, nothing
   else) and the `branch_exists` recovery (see Risks). `scripts/windows/open-pr-smoke.ps1 -ReportId <id>`
   calls the endpoint twice and prints both responses, for the acceptance walk
   (Windows setup ships in the PR). The dashboard gets no button (A2 owns the UI).
6. **Docs.** Roadmap Phase 8 entry; Phase 6's B2 bullet points at it; decision log
   entry for the write policy, the single-process claim assumption and R1 to R3.

**Out of scope**

- Any UI (A2), merge, auto-merge, review assignment, CI or preview wiring (S1).
- Re-running analysis when a diff does not apply (R2).
- Non-GitHub hosts, SSH remotes for this path, GitHub App auth (a later phase if
  ever needed). An SSH-cloned project is fine: this path never uses `origin`.
- Closing or updating a PR after it is opened; `fix_committed -> verified` stays
  manual and gated.

## Technical Approach

New module `src/bugalizer/git_ops/pull_request.py`, plus a thin route in
`api/reports.py`. Sequence for one call:

1. **Pre-mutation checks** (Scope 1): report exists; supplied proposal belongs to
   the report; report-wide recorded PR -> 200 / `pr_exists`; select the proposal;
   token configured; project cloned and on GitHub.
2. **Resolve the destination.** Parse `owner/repo` from `projects.repo_url`
   (`https://github.com/o/r(.git)` and `git@github.com:o/r(.git)` both map to `o/r`;
   owner and repo must match `[A-Za-z0-9_.-]+`; anything else -> 400). Every remote
   git command in this module uses the canonical URL
   `<github_web_base>/o/r.git` (`github_web_base` defaults to
   `https://github.com`) as an explicit argv destination. It never uses `origin`,
   so the clone's transport (HTTPS or SSH) and its `.git/config` do not matter and
   are never changed.
3. **Claim** (Scope 3): CAS `fix_proposed -> fix_approved` with a new token, or
   adopt an abandoned claim; register the token in-process. Everything after this
   runs in `try/finally`: on a handled failure, roll back with a token CAS; always
   deregister.
4. Under a per-project `asyncio.Lock`, with all git work in `asyncio.to_thread`:
   **cleanup of leftovers** for this report. If the worktree
   `<repos_dir>/.worktrees/<project-id>/<report-id>` is registered or its directory
   exists: `git worktree remove --force`, then `git worktree prune`, then remove the
   directory if it is still there. The path is resolved and asserted to lie under
   `<repos_dir>/.worktrees/` before any deletion. Also delete
   `refs/bugalizer/base/<report-id>` if present. These names are keyed by report id
   and only the current claim holder touches them, so the cleanup cannot hit
   another request's files.
5. **Reconcile with GitHub first**, before any apply or commit. Call
   `GET /repos/o/r/pulls?head=o:fix/bugalizer-<id>&state=all`. If a PR exists
   (open, closed or merged), first **resolve its owning proposal** among this
   report's proposals, from durable data only (never from the request's selection):
   1. the proposal whose recorded `pushed_sha` equals the PR's `head.sha` or
      appears in `GET /repos/o/r/pulls/<n>/commits` (the push intent from step 8;
      each proposal's commit is distinct because its message names the proposal,
      so at most one matches);
   2. else the proposal named by a `Bugalizer-Proposal: <id>` trailer, together
      with `Bugalizer-Report: <this report>`, in one of those commits (step 8
      writes both trailers, so this still works if the intent write was lost);
   3. else the PR is **unattributed**, for example a PR opened by hand from a
      hand-made branch. If the caller named a proposal, that explicit choice is the
      attribution (the operator recovery in Risks). If not, return
      `409 pr_unattributed` with the PR's URL and number, record nothing, and roll
      back.

   Commits and PR state come from the GitHub API, which keeps them for closed and
   merged PRs, so no fetch of the branch is needed. With the owner known, record the
   PR against the owner (step 10's atomic write, for the owner's proposal row only)
   and answer as the Scope 1 recorded-PR rule does: `200 created:false` with the
   owner's `fix_proposal_id`, or `409 pr_exists` if the caller named another
   proposal. This is how a crash after the PR POST recovers, and it works even when
   the PR was already merged and the diff no longer applies to the new base. If no
   PR exists, run `git ls-remote <canonical> refs/heads/fix/bugalizer-<id>`:
   - absent: continue to step 6;
   - present, with a tip equal to a `pushed_sha` recorded on one of this report's
     proposals: our push landed but the PR was never created. **Resume:** skip
     steps 6 to 8 and the push, and go straight to step 9's PR POST for that
     proposal. If the caller
     explicitly named a different proposal, return `409 branch_exists` naming the
     pushing proposal instead;
   - present with any other tip: `409 branch_exists`, left untouched.
6. **Fetch the base.** Run
   `git fetch --no-tags --no-write-fetch-head <canonical> refs/heads/<default_branch>:refs/bugalizer/base/<id>`
   in the analysis clone. The destination ref is private to this module, the
   refspec is non-forced, `--no-write-fetch-head` leaves `FETCH_HEAD` alone, and
   remote-tracking refs and remote config are not touched. (`--no-write-fetch-head`
   needs git 2.29 or later; `deploy-windows.md` states the minimum, and the module
   fails with a clear `502 github_error` detail on an older git rather than falling
   back to a fetch that writes `FETCH_HEAD`.) Resolve the base with
   `git rev-parse refs/bugalizer/base/<id>^{commit}`, then run
   `git worktree add --detach <worktree> <base-sha>`. The analysis clone's checkout,
   index and branches are never modified.
7. **Apply** in the worktree: `git apply --check`, then `git apply`, with strip
   level `-p1`, or `-p0` when the diff has no `a/` `b/` prefixes (decided from the
   `---` header, not by trial). A failure returns `409 diff_does_not_apply`.
8. **Commit detached.** `git add` only the paths in the applied diff, then commit
   with author `Bugalizer <bugalizer@localhost>` (configurable) and a message
   ending in the trailers `Bugalizer-Report: <report-id>` and
   `Bugalizer-Proposal: <proposal-id>`. HEAD stays detached. **Record the push
   intent:** write `pushed_sha = <commit>` and `branch_name` on the proposal before
   pushing, so step 5 can recognise the branch on a retry.
9. **Push**, after the policy guard:
   `git push <canonical> HEAD:refs/heads/fix/bugalizer-<id>` (no force; a
   non-fast-forward is rejected by the remote and maps to `branch_exists`). Then
   `POST /repos/o/r/pulls` with `base=<default_branch>`,
   `head=fix/bugalizer-<id>`, title `fix: <report title> (bugalizer <id>)`, and a
   body built from root cause, explanation, confidence, files changed, the tier and
   model that produced it, and the line "Opened by Bugalizer. A human reviews and
   merges; Bugalizer never merges." Not a draft, so CI and previews run (S1 depends
   on that). If the POST returns 422 "a pull request already exists", that is a
   race with our own earlier attempt: re-run step 5's lookup and record the PR it
   finds.
10. **Finalize atomically.** A single db helper
    `record_pull_request(report_id, proposal_id, …)` runs one SQLite transaction.
    `proposal_id` is the owner (the proposal just pushed, or the owner resolved in
    step 5), and the helper asserts it belongs to the report. The transaction sets
    that proposal's `pr_url`, `pr_number`,
    `pushed_sha`, `pr_opened_at` and `status = 'pr_opened'`, and moves the report
    `fix_approved -> fix_committed` with the claim token cleared, via a CAS on
    `status = 'fix_approved' AND claim_token = :mine`. The transaction commits both
    or neither. If the CAS matches no row, the claim was lost; the helper raises and
    nothing is written. Return 201.
11. `finally`: remove the worktree (`git worktree remove --force` + `prune`) and
    delete `refs/bugalizer/base/<id>`. A crash that skips this is handled by the
    next claimant's step 4.

**Token and remote handling.** The token is read once from
`BUGALIZER_GITHUB_TOKEN` as `SecretStr`. Every remote git command (fetch in step 6,
ls-remote in step 5, push in step 9) gets the same environment-only credential:
`GIT_CONFIG_COUNT=2`,
`http.<github_web_base>/.extraheader = AUTHORIZATION: basic <b64(x-access-token:token)>`
and `credential.helper=` (empty, so no host helper can prompt or leak), plus
`GIT_TERMINAL_PROMPT=0`. The credential never goes into a URL, argv,
`.git/config`, a log line, an exception message or a response. The GitHub REST
client uses httpx with the token in the `Authorization` header and a fixed
`github_api_base` (default `https://api.github.com`). Git stderr passes through the
same redaction before it reaches a log or an error detail. `/health` reports
`github_configured: bool`, never the value. `github_web_base` and
`github_api_base` are settings so that tests can aim at local servers; they are not
documented for operators.

## Files

- `src/bugalizer/git_ops/pull_request.py` (new): repo slug parsing, canonical URL,
  git env credential, leftover cleanup, worktree lifecycle, apply, detached commit,
  policy guard, push, GitHub client, redaction, claim registry.
- `src/bugalizer/api/reports.py`: `POST /reports/{id}/open-pr`.
- `src/bugalizer/models.py`: `OpenPrRequest`, `OpenPrResponse`; transition
  `FIX_APPROVED -> FIX_PROPOSED`; fix proposal response fields.
- `src/bugalizer/db.py`: five columns plus migration; token-aware claim, adopt and
  rollback CAS; report-wide PR lookup; proposal-of-report lookup; push-intent write;
  owner lookup by `pushed_sha`; atomic `record_pull_request` for a named owner.
- `src/bugalizer/config.py`: `github_token: SecretStr | None`, commit author name
  and email, `github_web_base`, `github_api_base`.
- `src/bugalizer/main.py`: `/health` `github_configured`.
- `tests/test_open_pr.py` (new); `tests/test_api.py` additions.
- `.env.example`, `docs/deploy-windows.md`, `scripts/windows/open-pr-smoke.ps1`.
- `docs/roadmap.md`, `docs/decision_log.md`, this file.

## Success Criteria

Test harness: git runs for real. The "GitHub" remote is a local bare repo served by
a stdlib HTTP server wrapping `git http-backend`. The server returns 401 unless the
request carries the expected `Authorization` header, and it records each request.
`github_web_base` points at it. The GitHub REST API is an httpx `MockTransport`
that records calls and holds PR state. The analysis clone is a normal clone of the
bare repo.

1. **Happy path:** a `fix_proposed` report yields 201. The bare repo has
   `fix/bugalizer-<id>` with exactly the diff's changes on top of the
   default-branch tip. The recorded PR request has the right base, head and body,
   and the report is `fix_committed`.
2. **Idempotency and ownership:** a second bodyless call returns 200 with the same
   PR and makes no push and no PR POST. With two proposals, where the older one was
   explicitly used to open the PR: a bodyless repeat returns 200 with that PR and
   the older `fix_proposal_id`; a repeat naming the newer proposal returns
   `409 pr_exists`; neither pushes nor POSTs. Another report's proposal ID and an
   unknown ID both return `404 proposal_not_found`, and the report's status, claim
   and proposals are unchanged.
3. **Restart recovery**, each simulating a process death by leaving persisted state
   as a crash would and clearing the in-process registry. Each is followed by one
   call that must return the right PR:
   - (a) *crash after claim*: report at `fix_approved` with a dead token and a
     leftover worktree directory plus base ref. The next call adopts the claim,
     cleans up the leftovers, and opens exactly one PR (one push, one POST) -> 201.
   - (b) *crash after PR POST, before the DB write*: branch on the bare repo, PR in
     the mock, DB at `fix_approved`. The next call -> 200 with that PR; zero pushes,
     zero POSTs; the report ends `fix_committed`.
   - (b') as (b), but the mock PR is merged and the default branch has moved so that
     the diff no longer applies. The result is still 200 with the same PR, and no
     apply is attempted.
   - (c) *finalization boundary*: `record_pull_request` fails mid-transaction
     (injected). Afterwards neither the proposal PR fields nor the report status
     changed, and the next call recovers as in (b). If the transaction committed,
     the next call gets 200 from the DB with no GitHub PR lookup.
   - (d) *ownership across proposals*: the report has proposals A (older) and B
     (newer). A call naming A pushes and opens the PR, then the process dies before
     `record_pull_request`. Two separate runs from that same persisted state: a
     bodyless retry returns 200 with `fix_proposal_id = A`; an explicit-B retry
     returns `409 pr_exists` naming A. In both, only A's row gets `pr_url`,
     `pr_number` and `pr_opened_at`, B's row is byte-identical, the report ends
     `fix_committed`, and there are zero pushes and zero PR POSTs. Both runs are
     repeated with the mock PR merged and the default branch moved on.
   - (e) *intent lost*: as (d), but A's `pushed_sha` was never written. The
     owner is still A, found through the commit trailers, with the same assertions.
   - (f) *unattributed PR*: a hand-made branch with a hand-opened PR whose commits
     have no trailer, and no recorded intent. A bodyless call returns
     `409 pr_unattributed` with the PR's URL, writes nothing, and leaves the report
     at `fix_proposed`. A call naming B then returns 200 with the PR recorded against
     B, with zero pushes and zero POSTs.
   - An active claim (token in the registry) makes a second call return
     `409 in_progress`, and the claim is not adopted.
4. **Failure-then-retry on the same clone, with no manual ref cleanup:**
   - push succeeds, PR POST returns 500 -> 502 and the report is back at
     `fix_proposed`. Retry: the branch tip matches the recorded `pushed_sha`, so the
     retry makes zero pushes and one POST -> 201.
   - push rejected by the server -> 502 and rollback. Retry after the server is
     fixed -> 201.
   After each run: `git branch --list` in the analysis clone is unchanged, no
   `refs/bugalizer/*` remain, and no worktree is registered or on disk.
5. **Stale diff:** a proposal whose context no longer matches the tip returns
   `409 diff_does_not_apply`. No ref appears on the bare repo, the only GitHub call
   is the step-5 PR lookup, no push is made, and the report is back at
   `fix_proposed`.
6. **Write policy:** no fetch, push or update-ref argv the module issues contains a
   force flag or `+` refspec (the only allowed `--force` is the exact
   `worktree remove` argv). A push aimed at the default branch or an unprefixed ref
   is refused by the guard (unit test with a forged ref). An existing remote branch
   with a foreign tip returns `409 branch_exists`, and the bare repo's ref is
   byte-identical afterwards.
7. **Destination and credentials:**
   - (a) With the analysis clone's `origin` set to an SSH URL
     (`git@github.com:o/r.git`, unreachable in the test), the call still succeeds.
     Every remote argv names the canonical `<github_web_base>/o/r.git`, and none
     names `origin`.
   - (b) Against the auth-checking HTTP server, fetch, ls-remote and push all
     succeed with the token configured. A control run with the header removed from
     the env gets 401, which proves the server really enforces auth.
   - (c) For every remote git call, the captured subprocess env carries the
     extraheader and the empty `credential.helper`.
   - (d) The clone's `.git/config` and remote list are byte-identical before and
     after.
8. **Concurrency:** two simultaneous calls on one report produce exactly one push
   and one PR. The other call gets `409 wrong_status` or `in_progress`.
9. **Secrecy:** with a sentinel token, the sentinel never appears in any response,
   captured log record, exception detail, `.git/config` of the clone or worktree,
   or git argv. This holds on success and on each failure path: apply failure,
   push failure with stderr, GitHub 422 and 500, `branch_exists`, and recovery.
10. **Isolation:** the analysis clone's HEAD, branch list, and `git status` are
    identical before and after every test path. A `FETCH_HEAD` written into the
    clone before the call is byte-identical afterwards, on the happy path and on
    the stale-diff, push-failure and fetch-failure paths. Every fetch argv carries
    `--no-write-fetch-head`.
11. `PATCH /reports/{id}/status` still refuses `fix_approved` and `fix_committed`,
    and `claim_token` is absent from every report and proposal response.
12. 503 with no token; 400 for a non-GitHub or uncloned project.
13. Full suite green in CI at the submitted commit. The `git http-backend` tests
    skip with a stated reason only if the binary is missing; CI's git ships it, and
    the submission reports the skip count.
14. **Acceptance (Greg, post-merge, BOWIE):** token set in the service env;
    `open-pr-smoke.ps1` on one real sonicgrid report opens a PR on
    `spherop/sonicgrid` with the analysis as the body and prints the same PR on the
    second call. Recorded in the roadmap entry. This is the direction record's B2
    exit criterion.

## Risks

- **A real PR on the shared sonicgrid repo is outward-facing.** Only the
  acceptance walk creates one, Greg runs it, and the PR says it was machine-opened.
- **LLM diffs may be malformed or fuzzy.** `git apply` is strict (no `--3way`, no
  fuzz); a bad diff fails as `diff_does_not_apply` rather than producing a
  surprising commit.
- **Token blast radius.** Limited by the fine-grained token's single-repo scope;
  the service cannot push to other repos even if a project row points elsewhere.
- **`branch_exists` needs a human.** The service never overwrites a branch it
  cannot prove it pushed. If the push succeeded but the PR POST failed, the
  recorded `pushed_sha` makes the retry resume automatically. Otherwise, for
  example a hand-made branch, or a push whose intent write was lost, the operator
  either opens the PR by hand on GitHub, or deletes the remote branch and calls
  again. The next call finds a hand-opened PR in step 5. If its commits carry
  Bugalizer's trailers, it is attributed automatically. If not, the call returns
  `pr_unattributed`, and the operator calls again naming the proposal the PR
  implements. `deploy-windows.md` documents all three.
- **Single-process assumption.** Abandoned-claim detection relies on one Bugalizer
  process per database, as the architecture already requires. It is written into
  the decision log so that any future multi-process change has to revisit it.
