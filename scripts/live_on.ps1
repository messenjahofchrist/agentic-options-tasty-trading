# Turn on unattended LIVE trading: the real-money LIVE engine starts by itself every weekday at 08:10 CT.
# Owner order 2026-11-18: "It should trade daily without my intervention unless I say stop."
#
#   powershell -ExecutionPolicy Bypass -File scripts\live_on.ps1             # on, and start now if the market is open
#   powershell -ExecutionPolicy Bypass -File scripts\live_on.ps1 -NoStartNow # on from the next weekday
#   powershell -ExecutionPolicy Bypass -File scripts\live_off.ps1            # stop (see that script)
#
# What stays checked on every start: the authorize-live record for the ACTIVE V2 hash (a new version still needs a
# new authorize-live), the account, the kill switch, broker reconciliation. A start that is blocked emails a
# critical alert (live_autostart_blocked). Never turn this on on two PCs: the lock only covers one PC.
param([switch]$NoStartNow)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$repo = (Get-Location).Path

& .venv\Scripts\python.exe -m trader.cli live-autostart on --note "Owner order 2026-11-18: trade daily without my intervention unless I say stop"
if ($LASTEXITCODE -ne 0) { throw "live-autostart on failed; LIVE was not scheduled" }

$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" `
          -Argument "/d /c `"$repo\scripts\live_engine.cmd`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "08:10"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun `
            -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
Register-ScheduledTask -TaskPath "\OptioBot\" -TaskName "LiveEngine" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description "REAL-MONEY LIVE V2 engine (small-account rules), weekdays 08:10 CT, owner order 2026-11-18. Off: scripts\live_off.ps1" | Out-Null
Enable-ScheduledTask -TaskPath "\OptioBot\" -TaskName "LiveEngine" | Out-Null
"LIVE autostart ON: \OptioBot\LiveEngine runs weekdays at 08:10 CT (next: $((Get-ScheduledTaskInfo -TaskPath '\OptioBot\' -TaskName 'LiveEngine').NextRunTime))"

$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "live-session" })
$now = Get-Date
$weekday = $now.DayOfWeek -notin @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)
if ($NoStartNow) { "Not started now (-NoStartNow)." }
elseif ($running.Count -gt 0) { "A LIVE session is already running (pid $($running.ProcessId -join ', ')); not starting another." }
elseif ($weekday -and $now.TimeOfDay -ge [TimeSpan]"07:00" -and $now.TimeOfDay -lt [TimeSpan]"14:30") {
    Start-ScheduledTask -TaskPath "\OptioBot\" -TaskName "LiveEngine"
    "Started LIVE now. Log: data\live\live_engine.log"
}
else { "Outside today's entry hours; LIVE starts at the next scheduled time." }
