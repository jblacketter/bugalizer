<#
.SYNOPSIS
  Keep Bugalizer's sonicgrid copy up to date automatically (Windows scheduled task).

.DESCRIPTION
  Registers the scheduled task "Bugalizer sonicgrid copy". Every -Minutes it runs
  check-sonicgrid-copy.ps1 -Fetch -Update as the current user:
  it fetches origin/main in the -Source sonicgrid checkout (your SSH key, no
  change to your working tree or branches) and resets Bugalizer's copy to it.
  The last run's output goes to <bugalizer checkout>\cache\sonicgrid-copy.log.

  The task uses logon type S4U, so it runs whether or not you are logged on
  and stores no password. That needs an elevated PowerShell; without one the
  script falls back to running only while you are logged on.

  Re-running replaces the task. -Remove deletes it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\register-sonicgrid-copy-task.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\register-sonicgrid-copy-task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [int]$Minutes = 30,
    [string]$Source = "C:\Users\jblac\projects\sonicgrid",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$taskName = "Bugalizer sonicgrid copy"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "REMOVED: $taskName" -ForegroundColor Green
    exit 0
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$check = Join-Path $PSScriptRoot "check-sonicgrid-copy.ps1"
$log = Join-Path $repoRoot "cache\sonicgrid-copy.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

$cmd = "& '$check' -Source '$Source' -Fetch -Update *> '$log'"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"$cmd`"" `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$user = "$env:USERDOMAIN\$env:USERNAME"

try {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    $mode = "whether or not $user is logged on"
} catch {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    $mode = "only while $user is logged on (run elevated for always-on)"
}

Write-Host "REGISTERED: '$taskName' every $Minutes min, $mode." -ForegroundColor Green
Write-Host "Last run's output: $log"
Write-Host "Run it now:        Start-ScheduledTask -TaskName '$taskName'"
