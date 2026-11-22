# Stop unattended LIVE trading: LIVE no longer starts by itself, and a running LIVE engine is stopped now.
# Owner order 2026-11-18: LIVE trades daily "unless I say stop". This is the stop.
#
#   powershell -ExecutionPolicy Bypass -File scripts\live_off.ps1
#
# Open LIVE positions are NOT sold: they stay at tastytrade with no bot managing them (no broker-side stop). The
# script lists them; close them in the tastytrade app, or turn LIVE back on (scripts\live_on.ps1), which resumes
# managing them. To stop for the rest of today only, press Ctrl+C in the LIVE window instead.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$task = Get-ScheduledTask -TaskPath "\OptioBot\" -TaskName "LiveEngine" -ErrorAction SilentlyContinue
if ($task) {
    Disable-ScheduledTask -TaskPath "\OptioBot\" -TaskName "LiveEngine" | Out-Null
    schtasks /End /TN "\OptioBot\LiveEngine" 2>$null | Out-Null
}
# Ending the task only stops its cmd.exe window; stop the engine's own python processes too.
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "live-session" })
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
"Stopped $($procs.Count) LIVE process(es); \OptioBot\LiveEngine disabled."

& .venv\Scripts\python.exe -m trader.cli live-autostart off
if ($LASTEXITCODE -ne 0) { throw "live-autostart off failed: delete live_autostart.json by hand" }
