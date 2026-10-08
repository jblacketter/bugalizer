# sonicgrid end-to-end run (tonight's checklist)

## The goal

One full end-to-end run of Bugalizer on sonicgrid, on current code:

1. A bug filed in sonicgrid
2. Bugalizer triages it
3. **Propose fix** writes a diff, using the admin's own Claude key
4. **Fix and open PR** opens a real PR on `spherop/sonicgrid`
5. Merging or closing that PR shows up on the bug in sonicgrid, and **Reopen** in sonicgrid
   sends the bug back to Bugalizer

**Already working** (passed 2026-10-07): steps 1–3 work. They ran on Greg's key; see the
Phase 12 notes in `roadmap.md`.

**Still missing:**

- **Bugalizer's sonicgrid copy was stale.** It sat at `d4be8626` (2026-09-14), 71 commits behind
  main. Fixes were written against old code, so their PRs would not apply.
- **BOWIE has no GitHub token** (`github_configured: false`), so step 4 cannot run.
- **`BUGALIZER_REOPEN_ENABLED=true`** is not yet set in sonicgrid's Vercel env. While it is
  off, sonicgrid hides the **Reopen** button on finished bugs in `/admin/bugs` and refuses to
  queue a reopen (`src/lib/bugalizer/reopen-flag.ts`). It was off on purpose: the older
  Bugalizer (B3) rejected reopen requests. B4 handles them and is live on BOWIE, so it can be
  turned on. Only step 5's reopen needs it.

## Why the copy matters

Bugalizer does **not** read `C:\Users\jblac\projects\sonicgrid`. When it localizes a bug and
proposes a fix, it reads its **own** copy at `<bugalizer checkout>\repos\3e300658671b445e`.
Nothing refreshes that copy automatically. `/projects/{id}/clone` and `/refresh-map` run a
`git pull` without GitHub credentials, and that fails on the private repo. Until automatic
refresh is built (a planned follow-up), `scripts\windows\check-sonicgrid-copy.ps1` checks the
copy and updates it from your local sonicgrid checkout.

## Steps (on BOWIE, in PowerShell)

### 1. Check the copy (you, 2 minutes)

```powershell
cd <the Bugalizer folder the service runs from>
git pull
git -C C:\Users\jblac\projects\sonicgrid pull
powershell -ExecutionPolicy Bypass -File scripts\windows\check-sonicgrid-copy.ps1
```

| Output | Meaning |
|---|---|
| `CURRENT` | Done. Go to step 2. |
| `BEHIND` or `DIFFERENT` | Re-run the script with `-Update` at the end. |
| `NOT FOUND` | You're in the wrong Bugalizer folder. The script lists Bugalizer's Windows services and the path each one runs from; `cd` there and run it again. |

Send Claude the output, including the folder path. That path will go into `deploy-windows.md`.

### 2. Dan creates the GitHub token (Dan, 5 minutes)

Follow **Part A** of `open-pr-acceptance.md`. It is a fine-grained token on `spherop/sonicgrid`
only, with **Contents** and **Pull requests** set to read and write. Only Dan can create it,
because `spherop` owns the repo. He sends it to you privately (a password-manager share).

In the same message, ask Dan to set **`BUGALIZER_REOPEN_ENABLED=true`** in sonicgrid's Vercel
env and redeploy.

### 3. Put the token on BOWIE (you)

Follow **Part B**, steps 1–4, of `open-pr-acceptance.md`:

1. Add `BUGALIZER_GITHUB_TOKEN=…` to `.env` (use Notepad).
2. Restart the service.
3. Run `check-service.ps1`.

Pass: `/health` shows `"github_configured": true`.

### 4. The run (you click; Claude watches BOWIE's API)

1. File a **fresh**, small, real bug in sonicgrid. Don't reuse the earlier one: its diff was
   written against September's code and would fail with `diff_does_not_apply`.
2. Wait for triage, then click **Propose fix** in `/admin/bugs`.
3. Click **Fix and open PR**. The PR should appear on `spherop/sonicgrid`.
4. Merge or close the PR on GitHub. Check that the bug shows it as merged or closed in
   `/admin/bugs`.
5. Click **Reopen** on that bug in `/admin/bugs` (needs the Vercel flag). Check that Bugalizer
   picks it up again.

Filing the bug and Propose fix (1 and 2 above) don't need the token, so you can start them
while waiting on Dan. Only Fix and open PR does.
