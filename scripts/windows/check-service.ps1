<#
.SYNOPSIS
  Post-deploy check for the Bugalizer service on the Windows host (BOWIE).

.DESCRIPTION
  Prints, in order:
    1. Revision: the checkout's git HEAD (or -ExpectedRevision) and, separately,
       the revision the RUNNING service reports on /health. The verdict is
       VERIFIED only when they match; STALE when the service runs a different
       commit; UNKNOWN when the service reports no revision. The script never
       infers "deployed" from the checkout alone.
    2. Health: GET /health/live and GET /health. A 503 body is parsed and shown
       (the service returns its `checks` even when the database check fails).
       Transport failures (nothing listening) are reported separately from
       HTTP failures.
    3. Auth: `auth_enabled` from /health. false = BUGALIZER_API_KEYS is empty.
       A service that omits the field predates Phase 7 -> reported as unknown.
    4. Ollama reachability (GET /api/tags on the configured host).
    5. Deploy option: a bugalizer Docker container (image id, started at) or a
       Windows service named Bugalizer (state, start time when available).

  Its output is the post-merge acceptance record: paste it into the phase's
  handoff entry or PR.

.PARAMETER BaseUrl
  Bugalizer base URL. Default http://127.0.0.1:8090.

.PARAMETER ExpectedRevision
  Revision to compare the running service against. Default: `git rev-parse
  HEAD` of the checkout this script lives in.

.PARAMETER OllamaHost
  Ollama base URL. Default: BUGALIZER_OLLAMA_HOST from the environment or the
  repo-root .env, else http://127.0.0.1:11434.

.PARAMETER SkipDeployOption
  Skip the Docker / Windows-service probe (for running the check from a
  machine that is not the host).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\check-service.ps1

.EXAMPLE
  pwsh -File scripts/windows/check-service.ps1 -BaseUrl http://bowie:8090 -ExpectedRevision abc1234 -SkipDeployOption
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8090",
    [string]$ExpectedRevision = "",
    [string]$OllamaHost = "",
    [switch]$SkipDeployOption
)

$ErrorActionPreference = "Continue"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$script:Problems = 0

function Write-Section($title) {
    Write-Host ""
    Write-Host "== $title ==" -ForegroundColor Cyan
}

function Write-Problem($text) {
    $script:Problems++
    Write-Host $text -ForegroundColor Red
}

function Get-Json($url) {
    <#
      Returns a hashtable:
        kind   = 'ok' (2xx, body parsed) | 'http' (non-2xx, body parsed when
                 JSON) | 'transport' (no HTTP response at all)
        status = HTTP status code when there was a response
        body   = parsed JSON (or $null)
        error  = message for transport failures / unparseable bodies
      Non-2xx bodies are preserved: /health returns JSON on 503.
    #>
    $iwr = Get-Command Invoke-WebRequest
    $canSkip = $iwr.Parameters.ContainsKey("SkipHttpErrorCheck")
    $status = $null
    $raw = $null
    try {
        if ($canSkip) {
            $resp = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing -SkipHttpErrorCheck
            $status = [int]$resp.StatusCode
            $raw = $resp.Content
        } else {
            $resp = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing
            $status = [int]$resp.StatusCode
            $raw = $resp.Content
        }
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) {
            return @{ kind = "transport"; status = $null; body = $null; error = $_.Exception.Message }
        }
        # Windows PowerShell 5.1: non-2xx throws; read the body off the response.
        try {
            $status = [int]$response.StatusCode
            $stream = $response.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $raw = $reader.ReadToEnd()
            $reader.Close()
        } catch {
            return @{ kind = "http"; status = $status; body = $null; error = "could not read response body: $($_.Exception.Message)" }
        }
    }
    $body = $null
    $parseError = $null
    if ($raw) {
        try { $body = $raw | ConvertFrom-Json } catch { $parseError = "non-JSON body" }
    }
    $kind = if ($status -ge 200 -and $status -lt 300) { "ok" } else { "http" }
    return @{ kind = $kind; status = $status; body = $body; error = $parseError }
}

function Format-Bool($value) {
    if ($null -eq $value) { return "unknown" }
    return "$value".ToLower()
}

# 1. Revision: checkout vs running service --------------------------------
Write-Section "Revision"
$checkoutSha = $null
$checkoutNote = ""
if ($ExpectedRevision) {
    $checkoutSha = $ExpectedRevision.Trim()
    $checkoutNote = "(-ExpectedRevision)"
} elseif (Get-Command git -ErrorAction SilentlyContinue) {
    Push-Location $repoRoot
    try {
        $checkoutSha = (git rev-parse HEAD 2>$null)
        $branch = (git rev-parse --abbrev-ref HEAD 2>$null)
        $dirty = (git status --porcelain 2>$null)
        if ($checkoutSha) {
            $checkoutNote = "($branch" + $(if ($dirty) { ", DIRTY tree" } else { "" }) + ")"
        }
    } finally { Pop-Location }
}
if ($checkoutSha) {
    Write-Host "checkout : $checkoutSha $checkoutNote"
} else {
    Write-Host "checkout : unknown (git unavailable or not a checkout; pass -ExpectedRevision)" -ForegroundColor Yellow
}

$live = Get-Json "$BaseUrl/health/live"
$runningSha = $null
if ($live.body -and ($live.body.PSObject.Properties.Name -contains "revision")) {
    $runningSha = $live.body.revision
}
if ($live.kind -eq "transport") {
    Write-Problem "running  : UNREACHABLE (transport: $($live.error))"
} elseif ($runningSha) {
    Write-Host "running  : $runningSha (reported by $BaseUrl/health/live)"
} else {
    Write-Host "running  : null (service reports no revision: image built without GIT_REVISION, or a pre-Phase-7 build)" -ForegroundColor Yellow
}

if ($live.kind -eq "transport") {
    Write-Problem "verdict  : NOT VERIFIED (service unreachable)"
} elseif (-not $runningSha -or -not $checkoutSha) {
    Write-Problem "verdict  : UNKNOWN (cannot establish what is running; deployment of $($checkoutSha) is NOT verified)"
} elseif ($runningSha -eq $checkoutSha) {
    Write-Host "verdict  : VERIFIED (running revision matches checkout)" -ForegroundColor Green
} else {
    Write-Problem "verdict  : STALE (service runs $runningSha, checkout is $checkoutSha; rebuild the image or restart the service)"
}

# 2. Health -----------------------------------------------------------------
Write-Section "Service health ($BaseUrl)"
switch ($live.kind) {
    "ok"        { Write-Host ("live     : {0} (version {1})" -f $live.body.status, $live.body.version) }
    "http"      { Write-Problem ("live     : HTTP {0}" -f $live.status) }
    "transport" { Write-Problem ("live     : UNREACHABLE (transport: {0})" -f $live.error) }
}

$ready = Get-Json "$BaseUrl/health"
$authEnabled = $null
$authKnown = $false
switch ($ready.kind) {
    "transport" {
        Write-Problem ("ready    : UNREACHABLE (transport: {0})" -f $ready.error)
    }
    default {
        $b = $ready.body
        if ($null -eq $b) {
            Write-Problem ("ready    : HTTP {0}, {1}" -f $ready.status, $(if ($ready.error) { $ready.error } else { "empty body" }))
        } else {
            $line = "ready    : {0} (HTTP {1})" -f $b.status, $ready.status
            if ($ready.kind -eq "ok") { Write-Host $line } else { Write-Problem $line }
            if ($b.checks) {
                $c = $b.checks
                Write-Host ("checks   : database={0} ollama={1} worker={2}" -f (Format-Bool $c.database), (Format-Bool $c.ollama), (Format-Bool $c.worker))
            } else {
                Write-Host "checks   : (none in body)" -ForegroundColor Yellow
            }
            if ($b.PSObject.Properties.Name -contains "auth_enabled") {
                $authKnown = $true
                $authEnabled = [bool]$b.auth_enabled
            }
        }
    }
}

# 3. Auth -------------------------------------------------------------------
Write-Section "Auth"
if ($authKnown) {
    if ($authEnabled) {
        Write-Host "auth     : auth_enabled=true" -ForegroundColor Green
    } else {
        Write-Problem "auth     : auth_enabled=false (BUGALIZER_API_KEYS is empty; set it before the service is reachable from the LAN)"
    }
} else {
    Write-Host "auth     : unknown (service did not report auth_enabled: pre-Phase-7 build, or /health unreachable)" -ForegroundColor Yellow
}

# 4. Ollama -----------------------------------------------------------------
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
if ($tags.kind -eq "ok" -and $tags.body) {
    $names = @($tags.body.models | ForEach-Object { $_.name })
    Write-Host ("reach    : ok ({0} model(s))" -f $names.Count)
    foreach ($n in $names) { Write-Host "           $n" }
} elseif ($tags.kind -eq "http") {
    Write-Problem ("reach    : HTTP {0}" -f $tags.status)
} else {
    Write-Problem ("reach    : UNREACHABLE (transport: {0})" -f $tags.error)
}

# 5. Deploy option ----------------------------------------------------------
if (-not $SkipDeployOption) {
    Write-Section "Deploy option"
    $found = $false
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $ids = @(docker ps --filter "name=bugalizer" --format "{{.ID}}" 2>$null)
        foreach ($id in $ids) {
            if (-not $id) { continue }
            $found = $true
            $info = docker inspect $id --format '{{.Name}}|{{.Image}}|{{.State.StartedAt}}|{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>$null
            $parts = "$info" -split '\|'
            Write-Host ("docker   : {0} image={1} started={2}" -f $parts[0].TrimStart('/'), $parts[1], $parts[2])
            if ($parts.Count -ge 4 -and $parts[3]) {
                Write-Host ("           image label revision={0}" -f $parts[3])
            } else {
                Write-Host "           image carries no revision label (built without GIT_REVISION)" -ForegroundColor Yellow
            }
        }
    }
    $svc = Get-Service -Name "Bugalizer" -ErrorAction SilentlyContinue
    if ($svc) {
        $found = $true
        $started = ""
        try {
            $proc = Get-CimInstance Win32_Service -Filter "Name='Bugalizer'" -ErrorAction Stop
            if ($proc.ProcessId) {
                $p = Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue
                if ($p) { $started = " started=" + $p.StartTime.ToString("s") }
            }
        } catch { }
        Write-Host ("nssm     : service '{0}' is {1}{2}" -f $svc.Name, $svc.Status, $started)
    }
    if (-not $found) {
        Write-Host "neither a bugalizer Docker container nor a Bugalizer Windows service was found" -ForegroundColor Yellow
    }
}

# Summary -------------------------------------------------------------------
Write-Host ""
if ($script:Problems -eq 0) {
    Write-Host "RESULT: OK" -ForegroundColor Green
    exit 0
} else {
    Write-Host ("RESULT: {0} problem(s), see above" -f $script:Problems) -ForegroundColor Red
    exit 1
}
