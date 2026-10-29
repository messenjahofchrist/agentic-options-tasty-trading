# Set the $1K PAPER ledger's per-trade cap (max_per_trade_dollars in profiles\small_account.json) and restart its engine
# if it is running. The ledger (balance, open trades) is never touched.
#
#   powershell -ExecutionPolicy Bypass -File scripts\set_small_account_bet.ps1 225   # owner choice 2026-10-29
#   powershell -ExecutionPolicy Bypass -File scripts\set_small_account_bet.ps1 150   # revert: the 2026-10-19 cap
param([Parameter(Mandatory = $true)][double]$Dollars)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if ($Dollars -le 0) { throw "The per-trade cap must be positive" }

$env:OPTIO_BET = "$Dollars"
& .venv\Scripts\python.exe -c @"
import json, os
from datetime import date
from pathlib import Path
path = Path('profiles/small_account.json')
data = json.loads(path.read_text(encoding='utf-8'))
small = data['profiles']['SMALL_ACCOUNT_PAPER']
dollars = float(os.environ['OPTIO_BET'])
small['max_per_trade_dollars'] = int(dollars) if dollars.is_integer() else dollars
small['max_per_trade_dollars_note'] = (f'Owner-authorized {date.today().isoformat()} via scripts/set_small_account_bet.ps1: no single '
    f'trade\'s modeled maximum loss (premium x 100 plus modeled round-trip costs) above `${dollars:g}. Removed at the `$10,000 transition.')
path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
print(f'max_per_trade_dollars = {dollars:g}')
"@
if ($LASTEXITCODE -ne 0) { throw "Updating profiles\small_account.json failed" }

$match = { param($cmd) $cmd -match "paper-session" -and $cmd -match "--account-profile small" }
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -and (& $match $_.CommandLine) })
if ($procs.Count -eq 0) { Write-Output "SmallAccountEngine is not running; it uses the new cap at its next scheduled start"; exit 0 }
schtasks /End /TN "\OptioBot\SmallAccountEngine" 2>$null | Out-Null
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
$deadline = (Get-Date).AddSeconds(30)
while ((Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -and (& $match $_.CommandLine) }) -and
       (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
schtasks /Run /TN "\OptioBot\SmallAccountEngine" | Out-Null
Write-Output "SmallAccountEngine restarted with the new cap"
