<#
.SYNOPSIS
  Point the sonicgrid project at sonicgrid's poll endpoint (Phase 10 / B1) and
  check that one poll works.

.DESCRIPTION
  Step 3 of docs/deploy-windows.md section 7c, as one command:

    1. Finds the sonicgrid project (the one project whose name or repo_url
       contains "sonicgrid"), unless -ProjectId is given.
    2. PATCHes its ingest_source / ingest_config. The body carries only the
       NAME of the variable that holds the poll token, never the token.
    3. Runs one poll now (POST .../ingest/run) and prints the ingest status.

  Verdict CONFIGURED when the service can see the token and the poll ended
  with no error. Safe to re-run: the same config again changes nothing and
  already-imported reports are never imported twice.

  Needs BUGALIZER_INGEST_ENABLED=true and SONICGRID_POLL_TOKEN in the
  service's .env (and a service restart) for the background poller; the poll
  this script runs works either way.

.PARAMETER ProjectId
  Bugalizer project to configure. Default: the single project matching
  "sonicgrid".

.PARAMETER BaseUrl
  Bugalizer base URL. Default http://127.0.0.1:8090 (run on BOWIE).

.PARAMETER ApiKey
  X-API-Key value. Default: $env:BUGALIZER_API_KEY, else the first key in
  BUGALIZER_API_KEYS from the environment or the repo-root .env.

.PARAMETER Url
  Sonicgrid poll endpoint. Default https://sonicgrid.co/api/bugalizer/bug-reports.

.PARAMETER CredentialEnv
  Name of the variable holding the poll token. Default SONICGRID_POLL_TOKEN.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\configure-sonicgrid-ingest.ps1
#>
[CmdletBinding()]
param(
    [string]$ProjectId = "",
    [string]$BaseUrl = "http://127.0.0.1:8090",
    [string]$ApiKey = "",
    [string]$Url = "https://sonicgrid.co/api/bugalizer/bug-reports",
    [string]$CredentialEnv = "SONICGRID_POLL_TOKEN"
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
                     TimeoutSec = 120; UseBasicParsing = $true }
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
Write-Host "project: $ProjectId"

# 2. Set the ingest config.
Write-Host ""
Write-Host "== Configure ==" -ForegroundColor Cyan
$payload = @{
    ingest_source = "supabase"
    ingest_config = @{ url = $Url; table = "bug_reports"; credential_env = $CredentialEnv }
} | ConvertTo-Json -Depth 3 -Compress
$patch = Invoke-Api "Patch" "/projects/$ProjectId" $payload
Write-Host "PATCH HTTP $($patch.status)"
if ($patch.status -ne 200) { Stop-With "PATCH answered HTTP $($patch.status): $($patch.raw)" }
Write-Host "ingest_source: $($patch.body.ingest_source)  url: $($patch.body.ingest_config.url)  credential_env: $($patch.body.ingest_config.credential_env)"

# 3. One poll now, then the status.
Write-Host ""
Write-Host "== Poll ==" -ForegroundColor Cyan
$run = Invoke-Api "Post" "/projects/$ProjectId/ingest/run" "{}"
Write-Host "POST ingest/run HTTP $($run.status)"
Write-Host $run.raw
$status = Invoke-Api "Get" "/projects/$ProjectId/ingest" $null
Write-Host ""
Write-Host "== Status ==" -ForegroundColor Cyan
Write-Host $status.raw

Write-Host ""
Write-Host "== Verdict ==" -ForegroundColor Cyan
if ($status.status -ne 200 -or -not $status.body) { Stop-With "GET ingest answered HTTP $($status.status)" }
$s = $status.body
if (-not $s.credential_present) {
    Stop-With "the service cannot see $CredentialEnv. Check the name in .env and restart the service."
}
if ($s.last_error) {
    $hints = @{
        "unauthorized"          = "token mismatch: $CredentialEnv here must equal BUGALIZER_POLL_TOKEN in Vercel production.";
        "source_not_configured" = "sonicgrid has no BUGALIZER_POLL_TOKEN set in Vercel production.";
        "network_error"         = "BOWIE could not reach $Url.";
        "timeout"               = "BOWIE could not reach $Url in time.";
    }
    $hint = $hints[$s.last_error]
    if (-not $hint) { $hint = "see docs/deploy-windows.md section 7c for last_error codes." }
    Stop-With "poll failed with $($s.last_error): $hint"
}
if (-not $s.enabled) {
    Write-Host "Note: BUGALIZER_INGEST_ENABLED is not true, so only manual polls run. Set it in .env and restart." -ForegroundColor Yellow
}
Write-Host "CONFIGURED: project $ProjectId polls $Url (imported so far: $($s.imported_total))." -ForegroundColor Green
exit 0
