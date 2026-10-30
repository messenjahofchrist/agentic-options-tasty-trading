# CREDIT_SPREADS (PAPER): switch new spreads on or off, install the 15-minute task, or switch to a saved version.
#
#   powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 on        # new spreads on
#   powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 off       # no new spreads; open ones are still managed to their exits
#   powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 install   # (re)create the CreditSpreads task: weekdays 08:45-15:15 CT, every 15 minutes
#   powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 remove    # delete the task (the bot stops completely, open spreads included)
#   powershell -ExecutionPolicy Bypass -File scripts\set_credit_spreads.ps1 1.0.0     # switch to a saved version in strategies\credit_spreads_versions\
#
# The ledgers in data\credit_spreads\ are never touched. The opening-range bots are not affected by any of these.
param([Parameter(Mandatory = $true)][string]$Action)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$config = "strategies\credit_spreads.json"
$python = (Resolve-Path ".venv\Scripts\python.exe").Path
$pythonw = (Resolve-Path ".venv\Scripts\pythonw.exe").Path

switch -Regex ($Action) {
    '^(on|off)$' {
        $flag = if ($Action -eq "on") { "true" } else { "false" }
        $text = Get-Content $config -Raw
        $new = [regex]::Replace($text, '"enabled":\s*(true|false)', '"enabled": ' + $flag, 1)
        [System.IO.File]::WriteAllText((Resolve-Path $config).Path, $new)
        & $python -c "from pathlib import Path; from trader.spreads import load_config; c = load_config(Path('.')); print('CREDIT_SPREADS', c['version'], 'new spreads enabled:', c['enabled'])"
        if ($LASTEXITCODE -ne 0) { throw "config check failed" }
    }
    '^install$' {
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "08:45"
        $repeat = New-ScheduledTaskTrigger -Once -At "08:45" -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 6 -Minutes 31)
        $trigger.Repetition = $repeat.Repetition
        $taskAction = New-ScheduledTaskAction -Execute $pythonw -Argument "-m trader.cli spreads-run" -WorkingDirectory (Get-Location).Path
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskPath "\OptioBot\" -TaskName "CreditSpreads" -Trigger $trigger -Action $taskAction -Settings $settings `
            -Description "OptioBot CREDIT_SPREADS PAPER pass (no broker orders)." -Force | Out-Null
        Write-Output "CreditSpreads task installed: weekdays 08:45-15:15 CT, every 15 minutes"
    }
    '^remove$' {
        Unregister-ScheduledTask -TaskPath "\OptioBot\" -TaskName "CreditSpreads" -Confirm:$false
        Write-Output "CreditSpreads task removed"
    }
    '^\d+\.\d+\.\d+$' {
        $source = "strategies\credit_spreads_versions\credit_spreads_$Action.json"
        if (-not (Test-Path $source)) { throw "No saved CREDIT_SPREADS version $Action ($source)" }
        Copy-Item $source $config -Force
        & $python -c "from pathlib import Path; from trader.spreads import load_config; c = load_config(Path('.')); print('CREDIT_SPREADS', c['version'], 'new spreads enabled:', c['enabled'])"
        if ($LASTEXITCODE -ne 0) { throw "config check failed" }
    }
    default { throw "Use on, off, install, remove, or a saved version number" }
}
