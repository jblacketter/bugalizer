<#
.SYNOPSIS
  Turn on the sonicgrid triage sync for the sonicgrid project (Phase 11 / B3)
  and check that one tick works.

.DESCRIPTION
  Step 2 of docs/sonicgrid-triage-acceptance.md, as one command:

    1. Finds the sonicgrid project (the one project whose name or repo_url
       contains "sonicgrid"), unless -ProjectId is given. It must already be
       configured for ingest (configure-sonicgrid-ingest.ps1).
    2. PATCHes its ingest_config with triage_credential_env, keeping the poll
       settings as they are (B1's checkpoint is not reset). The body carries
       only the NAME of the variable that holds the triage token.
    3. Runs one tick now (POST .../triage-sync/run) and prints the status.

  A tick is real: it pushes results to sonicgrid and claims and runs any
  pending admin actions. Check sonicgrid's open-action list first.

  Verdict CONFIGURED when the service can see the token and the tick ended
  with no error. Safe to re-run.

  Needs BUGALIZER_TRIAGE_SYNC_ENABLED=true, SONICGRID_TRIAGE_TOKEN and
  BUGALIZER_SONICGRID_CLOUD_USERS in the service's .env (and a restart) for
  the background loop; the tick this script runs works either way.

.PARAMETER ProjectId
  Bugalizer project to configure. Default: the single project matching
  "sonicgrid".

.PARAMETER BaseUrl
  Bugalizer base URL. Default http://127.0.0.1:8090 (run on BOWIE).

.PARAMETER ApiKey
  X-API-Key value. Default: $env:BUGALIZER_API_KEY, else the first key in
  BUGALIZER_API_KEYS from the environment or the repo-root .env.

.PARAMETER CredentialEnv
  Name of the variable holding the triage token. Default SONICGRID_TRIAGE_TOKEN.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\configure-sonicgrid-triage.ps1
#>
[CmdletBinding()]
param(
    [string]$ProjectId = "",
    [string]$BaseUrl = "http://127.0.0.1:8090",
    [string]$ApiKey = "",
    [string]$CredentialEnv = "SONICGRID_TRIAGE_TOKEN"
)

$ErrorActionPreference = "Continue"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")

function Get-ApiKey {
    if ($ApiKey) { return $ApiKey }
    if ($env:BUGALIZER_API_KEY) { return $env:BUGALIZER_API_KEY }
    $keys = $env:BUGALIZER_API_KEYS
    if (-not $keys) {
        $envFile = Join-Path $repoRoot ".env"
        if (Test-Path $envFile) {
            $line = Get-Content $envFile | Where-Object { $_ -match '^\s*BUGALIZER_API_KEYS\s*=' } | Select-Object -First 1
            if ($line) { $keys = ($line -split '=', 2)[1].Trim() }
        }
    }
    if ($keys) { return ($keys -split ',')[0].Trim() }
    return ""
}

function Invoke-Api($method, $path, $body) {
    <# Returns @{ status; raw; body } and never throws on non-2xx. #>
    $iwr = Get-Command Invoke-WebRequest
    $canSkip = $iwr.Parameters.ContainsKey("SkipHttpErrorCheck")
    $status = $null
    $raw = $null
    try {
        $params = @{ Uri = "$($BaseUrl.TrimEnd('/'))/api/v1$path"; Method = $method; Headers = $headers;
                     TimeoutSec = 300; UseBasicParsing = $true }
        if ($null -ne $body) { $params["ContentType"] = "application/json"; $params["Body"] = $body }
        if ($canSkip) { $params["SkipHttpErrorCheck"] = $true }
        $resp = Invoke-WebRequest @params
        $status = [int]$resp.StatusCode
        $raw = $resp.Content
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) {
            return @{ status = $null; raw = "transport failure: $($_.Exception.Message)"; body = $null }
        }
        $status = [int]$response.StatusCode
        try {
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
            $raw = $reader.ReadToEnd()
            $reader.Close()
        } catch {
            $raw = "(could not read response body)"
        }
    }
    $parsed = $null
    try { $parsed = $raw | ConvertFrom-Json } catch { }
    return @{ status = $status; raw = $raw; body = $parsed }
}

function Stop-With($message) {
    Write-Host "NOT CONFIGURED: $message" -ForegroundColor Red
    exit 1
}

$key = Get-ApiKey
$headers = @{}
if ($key) { $headers["X-API-Key"] = $key } else { Write-Host "No API key found; calling without X-API-Key." -ForegroundColor Yellow }

# 1. Find the project.
Write-Host "== Project ==" -ForegroundColor Cyan
if (-not $ProjectId) {
    $list = Invoke-Api "Get" "/projects" $null
    if ($list.status -ne 200) { Stop-With "GET /projects answered HTTP $($list.status): $($list.raw)" }
    $found = @($list.body.projects | Where-Object { ($_.name -match 'sonicgrid') -or ($_.repo_url -match 'sonicgrid') })
    if ($found.Count -ne 1) {
        $list.body.projects | ForEach-Object { Write-Host "  $($_.id)  $($_.name)  $($_.repo_url)" }
        Stop-With "expected exactly one sonicgrid project, found $($found.Count). Re-run with -ProjectId <id>."
    }
    $ProjectId = $found[0].id
}
$project = Invoke-Api "Get" "/projects/$ProjectId" $null
if ($project.status -ne 200) { Stop-With "GET project answered HTTP $($project.status): $($project.raw)" }
$cfg = $project.body.ingest_config
if (-not $project.body.ingest_source -or -not $cfg) {
    Stop-With "project $ProjectId has no ingest config. Run configure-sonicgrid-ingest.ps1 first."
}
Write-Host "project: $ProjectId  poll url: $($cfg.url)"

# 2. Add the triage credential name, keeping the poll settings.
Write-Host ""
Write-Host "== Configure ==" -ForegroundColor Cyan
$payload = @{
    ingest_config = @{
        url = $cfg.url; table = $cfg.table; credential_env = $cfg.credential_env;
        triage_credential_env = $CredentialEnv
    }
} | ConvertTo-Json -Depth 3 -Compress
$patch = Invoke-Api "Patch" "/projects/$ProjectId" $payload
Write-Host "PATCH HTTP $($patch.status)"
if ($patch.status -ne 200) { Stop-With "PATCH answered HTTP $($patch.status): $($patch.raw)" }
Write-Host "triage_credential_env: $($patch.body.ingest_config.triage_credential_env)"

# 3. One tick now, then the status.
Write-Host ""
Write-Host "== Tick ==" -ForegroundColor Cyan
$run = Invoke-Api "Post" "/projects/$ProjectId/triage-sync/run" "{}"
Write-Host "POST triage-sync/run HTTP $($run.status)"
Write-Host $run.raw
$status = Invoke-Api "Get" "/projects/$ProjectId/triage-sync" $null
Write-Host ""
Write-Host "== Status ==" -ForegroundColor Cyan
Write-Host $status.raw

Write-Host ""
Write-Host "== Verdict ==" -ForegroundColor Cyan
if ($status.status -ne 200 -or -not $status.body) { Stop-With "GET triage-sync answered HTTP $($status.status)" }
$s = $status.body
if (-not $s.credential_present) {
    Stop-With "the service cannot see $CredentialEnv. Check the name in .env and restart the service."
}
if ($s.last_error) {
    $hints = @{
        "unauthorized"            = "token mismatch: $CredentialEnv here must equal BUGALIZER_TRIAGE_TOKEN in Vercel production (not the poll token).";
        "source_not_configured"   = "sonicgrid has no BUGALIZER_TRIAGE_TOKEN set in Vercel production.";
        "network_error"           = "BOWIE could not reach sonicgrid.";
        "timeout"                 = "BOWIE could not reach sonicgrid in time.";
        "duplicate_triage_source" = "another project syncs the same sonicgrid; only one may.";
    }
    $hint = $hints[$s.last_error]
    if (-not $hint) { $hint = "see docs/sonicgrid-triage-acceptance.md for last_error codes." }
    Stop-With "tick failed with $($s.last_error): $hint"
}
if (-not $s.enabled) {
    Write-Host "Note: BUGALIZER_TRIAGE_SYNC_ENABLED is not true, so only manual ticks run. Set it in .env and restart." -ForegroundColor Yellow
}
Write-Host "CONFIGURED: project $ProjectId syncs with sonicgrid (results tracked: $($s.results_tracked))." -ForegroundColor Green
exit 0
