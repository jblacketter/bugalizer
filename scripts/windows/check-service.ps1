<#
.SYNOPSIS
  Post-deploy check for the Bugalizer service on the Windows host (BOWIE).

.DESCRIPTION
  Prints, in order:
    1. The deployed commit (git HEAD of the checkout this script lives in).
    2. GET /health/live and GET /health from the running service, including
       the Phase 7 `auth_enabled` flag (false means BUGALIZER_API_KEYS is empty
       and any LAN deployment must fix that before the Aegis proxy will start).
    3. Whether the configured Ollama host answers GET /api/tags.
    4. Which deploy option is active: Docker (a container from the compose
       project) or NSSM (a Windows service named Bugalizer).

  Its output is the post-merge acceptance record: paste it into the phase's
  handoff entry or PR.

.PARAMETER BaseUrl
  Bugalizer base URL. Default http://127.0.0.1:8090.

.PARAMETER OllamaHost
  Ollama base URL. Default: BUGALIZER_OLLAMA_HOST from the environment or the
  repo-root .env, else http://127.0.0.1:11434.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\check-service.ps1
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8090",
    [string]$OllamaHost = ""
)

$ErrorActionPreference = "Continue"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")

function Write-Section($title) {
    Write-Host ""
    Write-Host "== $title ==" -ForegroundColor Cyan
}

function Get-Json($url) {
    try {
        $resp = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing
        return @{ ok = $true; status = [int]$resp.StatusCode; body = ($resp.Content | ConvertFrom-Json) }
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        return @{ ok = $false; status = $status; error = $_.Exception.Message }
    }
}

# 1. Deployed commit -------------------------------------------------------
Write-Section "Deployed commit"
Push-Location $repoRoot
try {
    $sha = (git rev-parse --short HEAD 2>$null)
    $branch = (git rev-parse --abbrev-ref HEAD 2>$null)
    $dirty = (git status --porcelain 2>$null)
    if ($sha) {
        Write-Host "commit : $sha ($branch)"
        if ($dirty) { Write-Host "tree   : DIRTY" -ForegroundColor Yellow } else { Write-Host "tree   : clean" }
    } else {
        Write-Host "git not available or not a checkout: $repoRoot" -ForegroundColor Yellow
    }
} finally { Pop-Location }

# 2. Health ----------------------------------------------------------------
Write-Section "Service health ($BaseUrl)"
$live = Get-Json "$BaseUrl/health/live"
if ($live.ok) {
    Write-Host ("live   : {0} (version {1})" -f $live.body.status, $live.body.version)
} else {
    Write-Host ("live   : UNREACHABLE ({0})" -f $live.error) -ForegroundColor Red
}

$ready = Get-Json "$BaseUrl/health"
if ($ready.body) {
    $b = $ready.body
    Write-Host ("ready  : {0} (HTTP {1})" -f $b.status, $ready.status)
    $authColor = if ($b.auth_enabled) { "Green" } else { "Red" }
    Write-Host ("auth   : auth_enabled={0}" -f $b.auth_enabled) -ForegroundColor $authColor
    if (-not $b.auth_enabled) {
        Write-Host "         BUGALIZER_API_KEYS is empty. Set it before exposing the service on the LAN." -ForegroundColor Red
    }
    Write-Host ("checks : database={0} ollama={1} worker={2}" -f $b.checks.database, $b.checks.ollama, $b.checks.worker)
} else {
    Write-Host ("ready  : UNREACHABLE ({0})" -f $ready.error) -ForegroundColor Red
}

# 3. Ollama ----------------------------------------------------------------
if (-not $OllamaHost) {
    $OllamaHost = $env:BUGALIZER_OLLAMA_HOST
    if (-not $OllamaHost) {
        $envFile = Join-Path $repoRoot ".env"
        if (Test-Path $envFile) {
            $line = Get-Content $envFile | Where-Object { $_ -match '^\s*BUGALIZER_OLLAMA_HOST\s*=' } | Select-Object -First 1
            if ($line) { $OllamaHost = ($line -split '=', 2)[1].Trim().Trim('"') }
        }
    }
    if (-not $OllamaHost) { $OllamaHost = "http://127.0.0.1:11434" }
}
Write-Section "Ollama ($OllamaHost)"
$tags = Get-Json ($OllamaHost.TrimEnd('/') + "/api/tags")
if ($tags.ok) {
    $names = @($tags.body.models | ForEach-Object { $_.name })
    Write-Host ("reach  : ok ({0} model(s))" -f $names.Count)
    foreach ($n in $names) { Write-Host "         $n" }
} else {
    Write-Host ("reach  : UNREACHABLE ({0})" -f $tags.error) -ForegroundColor Red
}

# 4. Deploy option ---------------------------------------------------------
Write-Section "Deploy option"
$found = $false
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $containers = docker ps --filter "name=bugalizer" --format "{{.Names}}`t{{.Status}}" 2>$null
    if ($containers) {
        $found = $true
        Write-Host "docker : running"
        $containers | ForEach-Object { Write-Host "         $_" }
    }
}
$svc = Get-Service -Name "Bugalizer" -ErrorAction SilentlyContinue
if ($svc) {
    $found = $true
    Write-Host ("nssm   : service '{0}' is {1}" -f $svc.Name, $svc.Status)
}
if (-not $found) {
    Write-Host "neither a bugalizer Docker container nor a Bugalizer Windows service was found" -ForegroundColor Yellow
}

Write-Host ""
