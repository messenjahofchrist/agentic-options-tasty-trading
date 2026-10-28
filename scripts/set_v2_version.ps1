# Switch PROFESSIONAL_STRATEGY_V2 (PAPER) to a saved version and restart the engines that are running.
#
#   powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.0.0   # revert: V2 as deployed 2026-10-26
#   powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.1.0   # let winners run, breakeven fix, entry filters
#   powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.2.0   # 2.1.0 + no new entries in the last 30 minutes
#
# The saved versions live in strategies\v2_versions\. Ledgers (balances, open trades) are never touched: a running
# engine is stopped and started again with the chosen version and keeps managing its open trades.
param([Parameter(Mandatory = $true)][string]$Version)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$source = "strategies\v2_versions\professional_v2_$Version.json"
if (-not (Test-Path $source)) { throw "No saved V2 version $Version ($source)" }

Copy-Item $source strategies\professional_v2.json -Force
& .venv\Scripts\python.exe -m trader.cli v2-activate --approve --note "Owner switch to V2 $Version via scripts\set_v2_version.ps1"
if ($LASTEXITCODE -ne 0) { throw "v2-activate failed; the engines were not restarted" }

# Ending the scheduled task only stops its cmd.exe wrapper; the python engine keeps running (and keeps the log file
# open, so a new instance cannot start). Stop the engine's own python processes, found by their command line.
$engines = @{
    "SmallAccountEngine" = { param($cmd) $cmd -match "paper-session" -and $cmd -match "--account-profile small" }
    "PaperEngine"        = { param($cmd) $cmd -match "paper-session" -and $cmd -notmatch "--account-profile" }
}
foreach ($task in "SmallAccountEngine", "PaperEngine") {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
               Where-Object { $_.CommandLine -and (& $engines[$task] $_.CommandLine) })
    $state = (Get-ScheduledTask -TaskPath "\OptioBot\" -TaskName $task).State
    if ($procs.Count -eq 0 -and $state -ne "Running") {
        Write-Output "$task is not running; it starts on V2 $Version at its next scheduled time"
        continue
    }
    schtasks /End /TN "\OptioBot\$task" 2>$null | Out-Null
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -and (& $engines[$task] $_.CommandLine) }) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
    }
    schtasks /Run /TN "\OptioBot\$task" | Out-Null
    Write-Output "$task restarted on V2 $Version (stopped $($procs.Count) engine process(es))"
}
