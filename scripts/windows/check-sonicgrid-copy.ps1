<#
.SYNOPSIS
  Is Bugalizer's copy of sonicgrid up to date? Optionally bring it up to date.

.DESCRIPTION
  Bugalizer reads sonicgrid's code from its OWN copy at
  <bugalizer checkout>\repos\<project id>, not from C:\Users\jblac\projects\sonicgrid.
  This script compares that copy's HEAD with the -Source checkout's main and
  prints CURRENT, BEHIND or DIFFERENT.

  With -Update it brings the copy to the source's main by fetching from the
  LOCAL source checkout (no GitHub credentials needed) and resetting to it.
  It refuses if the copy has uncommitted changes.

  Run it from the Bugalizer checkout the service runs from. If the copy is
  not found there, it lists Windows services matching *bugal* and their
  paths so you can find the right folder.

.PARAMETER Source
  A sonicgrid checkout whose main is current. Default C:\Users\jblac\projects\sonicgrid.
  Pull it first (git -C <Source> pull).

.PARAMETER ProjectId
  Bugalizer's sonicgrid project id. Default 3e300658671b445e.

.PARAMETER Update
  Fetch the source's main into the copy and reset the copy to it.

.PARAMETER Fetch
  First run `git fetch origin main` in the source and compare against
  origin/main instead of the source's local main. This never touches the
  source's working tree or branches, so it is safe while you work there.
  The scheduled task (register-sonicgrid-copy-task.ps1) uses it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\check-sonicgrid-copy.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\check-sonicgrid-copy.ps1 -Update

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\check-sonicgrid-copy.ps1 -Fetch -Update
#>
[CmdletBinding()]
param(
    [string]$Source = "C:\Users\jblac\projects\sonicgrid",
    [string]$ProjectId = "3e300658671b445e",
    [switch]$Update,
    [switch]$Fetch
)

$ErrorActionPreference = "Continue"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$copy = Join-Path $repoRoot (Join-Path "repos" $ProjectId)

function Get-Head($path, $ref) {
    $out = & git -C $path rev-parse --short=8 $ref 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return "$out".Trim()
}

function Get-Line($path, $ref) {
    return (& git -C $path log -1 --format="%h  %cd  %s" --date=iso $ref 2>$null)
}

Write-Host ""
Write-Host "== Bugalizer's sonicgrid copy ==" -ForegroundColor Cyan
Write-Host "Bugalizer checkout: $repoRoot"
Write-Host "Copy:               $copy"

if (-not (Test-Path (Join-Path $copy ".git"))) {
    Write-Host "NOT FOUND: no git repo at the path above." -ForegroundColor Red
    Write-Host "This script must live in the Bugalizer checkout the service runs from."
    Write-Host "Windows services matching *bugal* (the path shows where it runs from):"
    Get-CimInstance Win32_Service | Where-Object { $_.Name -like "*bugal*" } |
        Select-Object Name, State, PathName | Format-List
    Write-Host "If nothing is listed, check Docker: docker ps --filter name=bugalizer"
    exit 2
}

$copyHead = Get-Head $copy "HEAD"
Write-Host "Copy HEAD:          $(Get-Line $copy 'HEAD')"

Write-Host ""
Write-Host "== Source (current sonicgrid main) ==" -ForegroundColor Cyan
Write-Host "Source:             $Source"
$srcRef = "main"
if ($Fetch) {
    # Updates only refs/remotes/origin/main; the working tree is untouched.
    & git -C $Source fetch --quiet origin main
    if ($LASTEXITCODE -ne 0) { Write-Host "fetch of origin main in $Source failed" -ForegroundColor Red; exit 2 }
    $srcRef = "refs/remotes/origin/main"
}
$srcMain = Get-Head $Source $srcRef
if (-not $srcMain) {
    Write-Host "Cannot read $srcRef in $Source. Pass -Source <a sonicgrid checkout>." -ForegroundColor Red
    exit 2
}
Write-Host "Source main:        $(Get-Line $Source $srcRef)"
if (-not $Fetch) { Write-Host "(Pull the source first if unsure: git -C $Source pull)" }

Write-Host ""
if ($copyHead -eq $srcMain) {
    Write-Host "CURRENT: the copy is at the source's main ($srcMain)." -ForegroundColor Green
    exit 0
}

& git -C $Source merge-base --is-ancestor $copyHead $srcMain 2>$null
if ($LASTEXITCODE -eq 0) {
    $behind = & git -C $Source rev-list --count "$copyHead..$srcMain"
    Write-Host "BEHIND: the copy is $behind commits behind the source's main." -ForegroundColor Yellow
} else {
    Write-Host "DIFFERENT: the copy ($copyHead) is not an ancestor of the source's main ($srcMain)." -ForegroundColor Yellow
}

if (-not $Update) {
    Write-Host "Re-run with -Update to bring it up to date."
    exit 1
}

Write-Host ""
Write-Host "== Updating the copy ==" -ForegroundColor Cyan
$dirty = & git -C $copy status --porcelain
if ($dirty) {
    Write-Host "REFUSED: the copy has uncommitted changes:" -ForegroundColor Red
    $dirty | ForEach-Object { Write-Host "  $_" }
    exit 1
}
& git -C $copy fetch $Source $srcRef
if ($LASTEXITCODE -ne 0) { Write-Host "fetch failed" -ForegroundColor Red; exit 1 }
& git -C $copy reset --hard FETCH_HEAD
if ($LASTEXITCODE -ne 0) { Write-Host "reset failed" -ForegroundColor Red; exit 1 }

$after = Get-Head $copy "HEAD"
if ($after -eq $srcMain) {
    Write-Host "UPDATED: the copy is now at $after." -ForegroundColor Green
    Write-Host "Bugalizer records the new commit on the next localization run; no restart needed."
    exit 0
}
Write-Host "Copy is at $after, expected $srcMain." -ForegroundColor Red
exit 1
