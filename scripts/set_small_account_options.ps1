# The $1K PAPER ledger's option-quality rules (rule_overrides in profiles\small_account.json). Restarts its engine if
# it is running. The ledger (balance, open trades) and strategy.txt are never touched; the $10K account is unaffected.
#
#   powershell -ExecutionPolicy Bypass -File scripts\set_small_account_options.ps1 tight      # owner choice 2026-11-02
#   powershell -ExecutionPolicy Bypass -File scripts\set_small_account_options.ps1 original   # revert: the strategy's own rules
#
# tight:    bid/ask gap at most 5% of the option's price (strategy: 10%), open interest at least 500 (100),
#           traded today at least 50 (10). The $0.10 gap ceiling, delta and expiration rules are unchanged.
param([Parameter(Mandatory = $true)][ValidateSet("tight", "original")][string]$Level)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$env:OPTIO_LEVEL = $Level
& .venv\Scripts\python.exe -c @"
import json, os
from datetime import date
from pathlib import Path
path = Path('profiles/small_account.json')
data = json.loads(path.read_text(encoding='utf-8'))
small = data['profiles']['SMALL_ACCOUNT_PAPER']
if os.environ['OPTIO_LEVEL'] == 'tight':
    small['rule_overrides'] = {'max_spread_fraction': 0.05, 'min_open_interest': 500, 'min_option_volume': 50}
    small['rule_overrides_note'] = (f'Owner-authorized {date.today().isoformat()} via scripts/set_small_account_options.ps1 tight: '
        'only options with a bid/ask gap of at most 5% of their price (strategy 10%), open interest >= 500 (100) and '
        'volume >= 50 (10). Tighten-only; removed at the `$10,000 transition.')
else:
    small.pop('rule_overrides', None)
    small.pop('rule_overrides_note', None)
path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
from trader.account_profiles import ProfileConfig
print('rule_overrides =', dict(ProfileConfig.load(Path('.'), 'small').small_rule_overrides) or 'none (the strategy''s own rules)')
"@
if ($LASTEXITCODE -ne 0) { throw "Updating profiles\small_account.json failed" }

$match = { param($cmd) $cmd -match "paper-session" -and $cmd -match "--account-profile small" }
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -and (& $match $_.CommandLine) })
if ($procs.Count -eq 0) { Write-Output "SmallAccountEngine is not running; it uses the new rules at its next scheduled start"; exit 0 }
schtasks /End /TN "\OptioBot\SmallAccountEngine" 2>$null | Out-Null
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
$deadline = (Get-Date).AddSeconds(30)
while ((Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -and (& $match $_.CommandLine) }) -and
       (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
schtasks /Run /TN "\OptioBot\SmallAccountEngine" | Out-Null
Write-Output "SmallAccountEngine restarted with the new option rules"
