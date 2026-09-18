<#
.SYNOPSIS
  Acceptance walk for open-pr (Phase 8 / B2): open a PR for one report, twice.

.DESCRIPTION
  Calls POST /api/v1/reports/<ReportId>/open-pr two times and prints each
  answer's HTTP status and JSON body. The first call opens the PR (201), or
  finds the one already opened (200). The second must return the same PR
  with `created: false`. The verdict is VERIFIED only then.

  This creates a real pull request on the project's GitHub repo. Run it on
  one report you mean to open a PR for. Bugalizer never merges; a human
  reviews and merges on GitHub.

  Its output is the acceptance record: paste it into the roadmap's Phase 8
  entry.

.PARAMETER ReportId
  The Bugalizer report to open a PR for (must be fix_proposed).

.PARAMETER FixProposalId
  Optional: the proposal to open. Default: the report's newest proposal.

.PARAMETER BaseUrl
  Bugalizer base URL. Default http://127.0.0.1:8090.

.PARAMETER ApiKey
  X-API-Key value. Default: $env:BUGALIZER_API_KEY, else the first key in
  BUGALIZER_API_KEYS from the environment or the repo-root .env.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\open-pr-smoke.ps1 -ReportId 3f2a9c0d1e4b5a6c
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReportId,
    [string]$FixProposalId = "",
    [string]$BaseUrl = "http://127.0.0.1:8090",
    [string]$ApiKey = ""
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

function Invoke-OpenPr($url, $headers, $body) {
    <# Returns @{ status; raw; body } and never throws on non-2xx. #>
    $iwr = Get-Command Invoke-WebRequest
    $canSkip = $iwr.Parameters.ContainsKey("SkipHttpErrorCheck")
    $status = $null
    $raw = $null
    try {
        $params = @{ Uri = $url; Method = "Post"; Headers = $headers; TimeoutSec = 180;
                     UseBasicParsing = $true; ContentType = "application/json"; Body = $body }
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

$key = Get-ApiKey
$headers = @{}
if ($key) { $headers["X-API-Key"] = $key } else { Write-Host "No API key found; calling without X-API-Key." -ForegroundColor Yellow }
$payload = if ($FixProposalId) { (@{ fix_proposal_id = $FixProposalId } | ConvertTo-Json -Compress) } else { "{}" }
$url = "$($BaseUrl.TrimEnd('/'))/api/v1/reports/$ReportId/open-pr"

Write-Host "open-pr smoke: $url"
Write-Host "body: $payload"
$results = @()
foreach ($n in 1, 2) {
    Write-Host ""
    Write-Host "== Call $n ==" -ForegroundColor Cyan
    $r = Invoke-OpenPr $url $headers $payload
    Write-Host "HTTP $($r.status)"
    Write-Host $r.raw
    $results += ,$r
}

Write-Host ""
Write-Host "== Verdict ==" -ForegroundColor Cyan
$first = $results[0]
$second = $results[1]
$firstOk = ($first.status -eq 201 -or $first.status -eq 200) -and $first.body -and $first.body.pr_url
$secondOk = ($second.status -eq 200) -and $second.body -and ($second.body.created -eq $false)
if ($firstOk -and $secondOk -and ($first.body.pr_url -eq $second.body.pr_url)) {
    Write-Host "VERIFIED: $($first.body.pr_url) (branch $($first.body.branch), proposal $($first.body.fix_proposal_id))" -ForegroundColor Green
    exit 0
}
Write-Host "NOT VERIFIED: expected 201/200 then 200 with the same pr_url and created=false." -ForegroundColor Red
exit 1
