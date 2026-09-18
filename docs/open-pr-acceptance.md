# open-pr acceptance walk (Phase 8, SC14)

Status: **pending, blocked on the GitHub token.** Phase 8 merged 2026-09-18
(PR #5, `5e7caab`). The walk proves one real PR can be opened on
`spherop/sonicgrid` from the BOWIE deployment.

Why it is blocked: `BUGALIZER_GITHUB_TOKEN` must be a fine-grained token
scoped to `spherop/sonicgrid` only (ruling R1). GitHub only lets the account
that owns a repo issue a fine-grained token for it. `spherop` is **Dan's**
personal account; Greg is a collaborator with push access, which is not
enough. So Dan creates the token (Part A) and Greg does the rest (Part B).

Do not substitute a classic `repo`-scope token from another account: it
reaches every repo that account can push to, which breaks R1.

## Part A: Dan (owner of `spherop`) creates the token

1. On github.com, signed in as `spherop`: profile picture ▸ **Settings** ▸
   **Developer settings** (bottom of the left sidebar) ▸ **Personal access
   tokens** ▸ **Fine-grained tokens** ▸ **Generate new token**
   (direct link: <https://github.com/settings/personal-access-tokens/new>).
2. **Token name:** `bugalizer-open-pr-bowie`
3. **Expiration:** 90 days (note the rotation date).
4. **Resource owner:** `spherop`
5. **Repository access:** *Only select repositories* ▸ `spherop/sonicgrid`
6. **Repository permissions:**
   - **Contents:** Read and write
   - **Pull requests:** Read and write
   - **Metadata:** Read-only (GitHub adds this automatically; it is required)
   - everything else: *No access*
7. **Generate token**, copy it (`github_pat_…`; shown once), and send it to
   Greg over a private channel (password-manager share). Not email or chat.

What Dan should know: PRs Bugalizer opens show `spherop` as the author and say
in the body that they were opened by Bugalizer; Bugalizer never merges, never
force-pushes and never writes to the default branch. Dan can revoke the token
at any time from the same page.

## Part B: Greg, on BOWIE

1. **Put the token in the service's `.env`.** Use Notepad so the token stays
   out of shell history:

   ```powershell
   cd C:\path\to\bugalizer        # the deployed checkout
   notepad .env
   ```

   Add `BUGALIZER_GITHUB_TOKEN=github_pat_…`, save. (`.env` is gitignored.)

2. **Native service only:** `git --version` must be 2.29 or later. (The
   Docker image ships git 2.39.)

3. **Deploy `main` (at least `5e7caab`) and restart** so the service reads
   the token and runs the Phase 8 code:
   - Docker:
     ```powershell
     git pull
     $env:GIT_REVISION = (git rev-parse HEAD)
     docker compose up -d --build
     ```
   - NSSM / LAN Service Manager:
     ```powershell
     git pull
     uv sync
     nssm restart Bugalizer          # or restart the lan-mgr-bugalizer service
     ```

4. **Check the deploy:**

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\check-service.ps1
   ```

   Expect `VERIFIED`, and `/health` must show `"github_configured": true`.

5. **Pick one real report** in the sonicgrid project that is `fix_proposed`
   and whose diff you are happy to see as a PR on `spherop/sonicgrid`.

6. **Run the walk** (creates a real PR; this is also the script's first run
   anywhere, so a syntax error would surface here):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\open-pr-smoke.ps1 -ReportId <report_id>
   ```

   Expect call 1 → `201` (or `200` if already opened), call 2 → `200` with the
   same `pr_url` and `created: false`, then `VERIFIED`. Error codes and the
   `branch_exists` recovery are in `deploy-windows.md` §7b.

7. **Record it:** paste the script output into the roadmap's Phase 8 entry
   and mark the phase complete. Leave the PR on GitHub for a human to review
   and merge (or close).
