# Install (or remove) the 2.9 side test tasks (owner, 2026-11-12). 2.8.0's tasks are not touched.
#
#   V29Engine10K     weekdays 08:40  2.9 on its $10K PAPER ledger (waits for the 09:45 ET stocks-in-play scan)
#   V29Engine1K      weekdays 08:47  2.9 on its $1K PAPER ledger (reads the scan the $10K engine wrote)
#   V29Report        weekdays every minute 08:25-15:35: data\v29_dashboard.html and data\compare_28_vs_29.html
#   V29Baseline      weekdays 19:30  this month's history for the scan list + tomorrow's opening-volume baseline
#   ResourceMonitor  weekdays every 5 minutes 08:20-15:20: memory/CPU of every bot process -> data\resource_usage.jsonl
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_v29_tasks.ps1            # install / update
#   powershell -ExecutionPolicy Bypass -File scripts\install_v29_tasks.ps1 -Remove    # remove all five
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$PythonW = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw "Project Python was not found: $Python" }
$names = 'V29Engine10K', 'V29Engine1K', 'V29Report', 'V29Baseline', 'ResourceMonitor'

$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder('\OptioBot')
if ($Remove) {
    foreach ($n in $names) { try { $folder.DeleteTask($n, 0); Write-Output "Removed \OptioBot\$n" } catch {} }
    return
}
New-Item -ItemType Directory -Force (Join-Path $RepoRoot 'data\v29\original_paper'), (Join-Path $RepoRoot 'data\v29\small_account_paper'), (Join-Path $RepoRoot 'data\in_play') | Out-Null

function Register([string]$Name, [string]$Description, [string]$Path, [string]$Arguments, [int]$Hour, [int]$Minute,
                  [string]$Every = '', [string]$For = '', [string]$Limit = 'PT1H', [bool]$CatchUp = $true) {
    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = $Description
    $definition.Settings.MultipleInstances = 2          # ignore a new run while one is still going
    $definition.Settings.StartWhenAvailable = $CatchUp
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.ExecutionTimeLimit = $Limit
    $definition.Settings.Priority = 7
    $definition.Principal.UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $definition.Principal.LogonType = 3
    $definition.Principal.RunLevel = 0
    $action = $definition.Actions.Create(0)
    $action.Path = $Path
    $action.Arguments = $Arguments
    $action.WorkingDirectory = $RepoRoot
    $trigger = $definition.Triggers.Create(3)
    $trigger.DaysOfWeek = 62                            # Monday-Friday
    $trigger.WeeksInterval = 1
    $trigger.StartBoundary = (Get-Date).Date.AddHours($Hour).AddMinutes($Minute).ToString('yyyy-MM-ddTHH:mm:ss')
    if ($Every) { $trigger.Repetition.Interval = $Every; $trigger.Repetition.Duration = $For }
    $folder.RegisterTaskDefinition($Name, $definition, 6, $null, $null, 3, $null) | Out-Null
    $info = Get-ScheduledTaskInfo (Get-ScheduledTask -TaskPath '\OptioBot\' -TaskName $Name)
    Write-Output "Installed \OptioBot\$Name; next run $($info.NextRunTime)"
}

$cmd = "$env:SystemRoot\System32\cmd.exe"
$log10 = Join-Path $RepoRoot 'data\v29\original_paper\paper_engine.log'
$log1 = Join-Path $RepoRoot 'data\v29\small_account_paper\paper_engine.log'
Register 'V29Engine10K' 'OptioBot V2 2.9.0 side test, $10K PAPER ledger (data\v29\original_paper). 2.8.0 is not affected.' `
    $cmd "/d /c `"`"$Python`" -u -m trader.cli side-session --ledger 10k >> `"$log10`" 2>&1`"" 8 40 -Limit 'PT8H' -CatchUp $false
Register 'V29Engine1K' 'OptioBot V2 2.9.0 side test, $1K PAPER ledger (data\v29\small_account_paper). 2.8.0 is not affected.' `
    $cmd "/d /c `"`"$Python`" -u -m trader.cli side-session --ledger 1k >> `"$log1`" 2>&1`"" 8 47 -Limit 'PT8H' -CatchUp $false
Register 'V29Report' 'OptioBot 2.9 live page and 2.8 vs 2.9 face-off page (read-only).' `
    $PythonW '-m trader.cli side-report' 8 25 -Every 'PT1M' -For 'PT7H10M' -Limit 'PT5M' -CatchUp $false
Register 'V29Baseline' 'OptioBot 2.9: download this month of 1-minute history for the scan list and rebuild the opening-volume baseline.' `
    $cmd "/d /c `"`"$Python`" -u -m trader.v2.in_play baseline >> `"$(Join-Path $RepoRoot 'data\in_play\baseline.log')`" 2>&1`"" 19 30 -Limit 'PT2H'   # Massive has the day's bars by ~19:00 CT, not at 16:45
Register 'ResourceMonitor' 'OptioBot: sample memory and CPU of the bot processes every 5 minutes (read-only).' `
    "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $RepoRoot 'scripts\resource_sample.ps1')`"" 8 20 -Every 'PT5M' -For 'PT7H' -Limit 'PT2M' -CatchUp $false
