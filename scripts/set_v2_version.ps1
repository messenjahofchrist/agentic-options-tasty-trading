# Switch PROFESSIONAL_STRATEGY_V2 (PAPER) to a saved version and restart the engines that are running.
#
#   powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.0.0   # revert: V2 as deployed 2026-10-26
#   powershell -ExecutionPolicy Bypass -File scripts\set_v2_version.ps1 2.1.0   # let winners run + trend filter
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

foreach ($task in "SmallAccountEngine", "PaperEngine") {
    $state = (Get-ScheduledTask -TaskPath "\OptioBot\" -TaskName $task).State
    if ($state -eq "Running") {
        schtasks /End /TN "\OptioBot\$task" | Out-Null
        Start-Sleep -Seconds 3
        schtasks /Run /TN "\OptioBot\$task" | Out-Null
        Write-Output "$task restarted on V2 $Version"
    } else {
        Write-Output "$task is not running ($state); it starts on V2 $Version at its next scheduled time"
    }
}
