# BOWIE prep for the open-pr acceptance walk

For the agent or operator on BOWIE (the Windows host). Run each block in
PowerShell from the deployed Bugalizer checkout, report the outputs listed
under **Report back**, and stop at any **STOP**. Nothing here needs the GitHub
token; that comes later from Dan (see `open-pr-acceptance.md`).

Goal: (1) the running service has the Phase 8 code, and (2) there is a real
`fix_proposed` sonicgrid report ready to become the acceptance PR.

## 1. Is the Phase 8 code actually running?

"Nothing new to deploy" is only true if the *running* service started from
the Phase 8 merge (`5e7caab`) or later. Commits after it (`44c1b53` and this
doc) are documentation only. Check what is running, not just the checkout:

```powershell
git fetch origin
git log --oneline -1 origin/main
git log --oneline -1 HEAD
$h = Invoke-RestMethod http://127.0.0.1:8090/health
"revision:          $($h.revision)"
"github_configured: $($h.github_configured)"
"auth_enabled:      $($h.auth_enabled)"
```

Decide:

| `github_configured` | `revision` | Meaning | Action |
|---|---|---|---|
| empty / missing | any | Pre-Phase-8 code is running | Deploy (step 2) |
| `False` | `5e7caab…` or later | Phase 8 is live | No deploy needed; skip to step 3 |
| `False` | empty / null | Phase 8 is live, revision unknown | Optional redeploy for a clean VERIFIED |

Also: if `HEAD` is behind `origin/main` and older than `5e7caab`, the checkout
was never pulled. Deploy (step 2).

**STOP** if `auth_enabled` is `False`: set `BUGALIZER_API_KEYS` in `.env`
before anything else.

## 2. Deploy (only if step 1 says so)

```powershell
git checkout main
git pull --ff-only
git log --oneline -1
```

Then restart the way this host runs the service. Find out which first:

```powershell
docker ps --filter name=bugalizer --format "{{.Names}} {{.Status}}" 2>$null
Get-Service *bugal* -ErrorAction SilentlyContinue | Select-Object Name, Status
```

- **Docker** (a `bugalizer` container is listed):
  ```powershell
  $env:GIT_REVISION = (git rev-parse HEAD)
  docker compose up -d --build
  ```
- **Windows service** (a `*bugal*` service is listed):
  ```powershell
  git --version     # must be 2.29 or later for open-pr
  uv sync
  Restart-Service <name from Get-Service above>
  ```

Wait about 10 seconds, then re-run step 1. Expect `github_configured: False`.

## 3. Deploy check

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\check-service.ps1
```

Expect `VERIFIED`. `STALE` with the running revision at `5e7caab` or later
and `HEAD` ahead of it only by documentation commits is acceptable; say so in
the report.

## 4. The sonicgrid project

The API key is the first entry of `BUGALIZER_API_KEYS` in `.env`; this reads
it without printing it:

```powershell
$line = Get-Content .env | Where-Object { $_ -match '^\s*BUGALIZER_API_KEYS\s*=' } | Select-Object -First 1
$K = (($line -split '=', 2)[1] -split ',')[0].Trim()
$hdr = @{ "X-API-Key" = $K }
$H = "http://127.0.0.1:8090/api/v1"
(Invoke-RestMethod "$H/projects" -Headers $hdr).projects |
  Select-Object id, name, repo_url, repo_path, default_branch | Format-List
```

The sonicgrid row must have:

- `repo_url` = `https://github.com/spherop/sonicgrid` (a `.git` suffix or
  `git@github.com:spherop/sonicgrid.git` is also fine). Anything else: **STOP**
  and report it; open-pr would answer 400.
- `repo_path` set. If empty, clone it:
  `Invoke-RestMethod -Method Post "$H/projects/<project_id>/clone" -Headers $hdr`
- `default_branch` = `main`.

## 5. A candidate report

```powershell
$P = "<sonicgrid project id from step 4>"
(Invoke-RestMethod "$H/reports?project_id=$P&status=fix_proposed" -Headers $hdr).reports |
  Select-Object id, title, updated_at | Format-Table -AutoSize
```

For each candidate (newest first), show its latest proposal:

```powershell
$R = "<report id>"
(Invoke-RestMethod "$H/reports/$R/fix_proposals" -Headers $hdr).fix_proposals[0] |
  Select-Object id, created_at, confidence, files_changed, root_cause, diff | Format-List
```

Greg picks one he is comfortable seeing as a real PR on `spherop/sonicgrid`.
Prefer a recent proposal: if sonicgrid's `main` has moved under the diff,
open-pr answers `409 diff_does_not_apply` (harmless: nothing is pushed).

**If there is no `fix_proposed` report:** list triaged ones
(`status=triaged`) and run a cloud analysis on one from the dashboard
(**Analyze (cloud)**; a paid call, needs `BUGALIZER_ANTHROPIC_API_KEY`). If it
reports a stale localization, refresh the clone (the `/clone` POST in step 4),
run **Analyze (local)**, then cloud again. Do not do this without Greg's go:
it spends money.

## Report back

1. Step 1: `origin/main`, `HEAD`, `revision`, `github_configured`,
   `auth_enabled`, and whether a deploy was needed.
2. Step 3: the verdict line from `check-service.ps1`.
3. Step 4: the sonicgrid project's `id`, `repo_url`, `repo_path` set or not,
   and `default_branch`.
4. Step 5: the chosen report id and proposal id, or "none in fix_proposed".

Never print or paste the API key or any GitHub token.
