# BOWIE prep report: open-pr acceptance walk (2026-09-17)

Result of running [`bowie-open-pr-prep.md`](bowie-open-pr-prep.md) on BOWIE.
Steps 1-4 pass. Step 5 produced a `fix_proposed` candidate, but the proposal
is a hallucination (see below) and would not survive open-pr. A fresh,
still-reproducible bug is needed before the acceptance PR.

## Step 1: running code

| Item | Value |
|---|---|
| `origin/main` | `3014c69` |
| `HEAD` | `3014c69` |
| running `revision` (before) | `44c1b53` (already Phase 8) |
| running `revision` (after restart) | `3014c69` |
| `github_configured` | `False` |
| `auth_enabled` | `True` |
| git | 2.50.1 (open-pr needs 2.29+) |

Deploy was not strictly required (44c1b53 is after the Phase 8 merge), but
the service was restarted via lan-mgr for a clean VERIFIED.

## Step 3: deploy check

`check-service.ps1`: **VERIFIED** (running revision matches checkout).
Checks: database=true, ollama=true, worker=true. `RESULT: OK`.

## Step 4: the sonicgrid project

There was no sonicgrid project on the instance (only `smoke`, pointing at the
bugalizer repo itself). Created one:

| Field | Value |
|---|---|
| id | `3e300658671b445e` |
| repo_url | `https://github.com/spherop/sonicgrid.git` |
| repo_path | `repos\3e300658671b445e` (set) |
| default_branch | `main` |
| head_sha | `d4be86260ca52468533b5750c355e29fe4504af9` |

How the clone was made: `spherop/sonicgrid` is private and the service has no
GitHub credentials, so `POST /clone` cannot fetch it. Instead the existing
local checkout at `C:\Users\jblac\projects\sonicgrid` was fast-forwarded
(was 10 commits behind, clean) and cloned into `repos\<id>` with
`git clone --branch main --single-branch <local path>`. The copy's `origin`
was then pointed at the GitHub HTTPS URL, and `repo_path` + `head_sha` were
written with `db.project_update`. This is valid for open-pr, which never
uses `origin`: every remote command names the canonical GitHub URL and
carries the token in the environment.

Caveat: `POST /refresh-map` (which does `git pull`) will fail on this clone
until the service has GitHub credentials. Re-clone from the local checkout
to refresh instead.

## Step 5: candidate report

Filed GitHub issue spherop/sonicgrid#100 ("Selecting Tracks + button from
landing page creates empty bogus tracks") as a Bugalizer report.

| Item | Value |
|---|---|
| report id | `f8c0a9dbbe304f93` |
| proposal id | `24c9e5b49e9c4c65` |
| status | `fix_proposed` |
| localization `repo_sha` | `d4be8626` (matches `head_sha`; SHA-fresh) |
| Stage 4 provider | global Ollama (`ollama/qwen2.5-coder:14b`); no paid call |
| timing | validate+triage+localize ~25 s; Stage 4 ~20 s |

Note: the `.env` on BOWIE sets `BUGALIZER_FIX_PROVIDER=ollama`, so the
dashboard's "Analyze (cloud)" runs Stage 4 locally and free here. No
Anthropic key is set.

### Why the proposal is unusable

- The diff edits `src/components/LandingPage.tsx`, which does not exist.
  It also imports `react-router-dom` and `antd`; sonicgrid uses the Next.js
  app router and neither library.
- Localization never found the real surface: pass 1 picked a time-formatting
  helper (`src/app/track/[id]/edit/lib/format.ts`) and `src/types/track.ts`;
  pass 2 settled on the `Track` interface.
- Root cause: issue #100 is already fixed on current main. `src/app/track/new/page.tsx`
  is now a compatibility redirect whose comment describes exactly this bug
  ("minted a phantom Untitled Track"); creation goes through `useCreateTrack()`
  behind a gesture. There was nothing in the code for the models to anchor on.
  The other labeled bugs (#83, #101) are from mid-2025 and look equally stale
  (#101's "Upload audio" button is wired to a file picker now).

If open-pr were run on this proposal it would fetch `main`, fail to apply the
diff, and answer `409 diff_does_not_apply` without pushing anything. That is
a valid negative test of the guard, not the PR-opening walk.

## Recommendation

File a bug that still reproduces on current sonicgrid `main` and names a
concrete component or file; then run local analysis, then Stage 4. Consider
setting the project's `llm_model` to `qwen2.5-coder:14b` for the local stages
(it currently uses the `7b` default) to improve localization on the large
Next.js tree.

## Housekeeping done on BOWIE

- `.env`: the GitHub token key was misspelled `BUGALYZER_GITHUB_TOKEN` (the
  service would never have read it). Renamed to `BUGALIZER_GITHUB_TOKEN`;
  value still empty. A UTF-8 BOM introduced while rewriting the file was
  stripped (pydantic-settings would otherwise mangle the first key).
- Pitfall for anyone scripting the checklist in PowerShell: variables are
  case-insensitive, so `$r = Invoke-RestMethod ...` overwrites `$R`.
