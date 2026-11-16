# Nightly option-quote recorder (owner-approved 2026-11-08): read-only market data, never orders.
# Registers \OptioBot\OptionRecorder, Monday-Friday at 15:45 local time (this machine runs on Central time, so 16:45 ET,
# after the engines, the end-of-day jobs and the credit-spread passes). On a market holiday it records nothing new.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_option_recorder.ps1            # install or update
#   powershell -ExecutionPolicy Bypass -File scripts\install_option_recorder.ps1 -Remove    # remove the task
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Log = Join-Path $RepoRoot "data\option_history\recorder.log"

$service = New-Object -ComObject "Schedule.Service"
$service.Connect()
$folder = $service.GetFolder("\OptioBot")
if ($Remove) {
    $folder.DeleteTask("OptionRecorder", 0)
    Write-Output "Removed \OptioBot\OptionRecorder"
    return
}
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null
$definition = $service.NewTask(0)
$definition.RegistrationInfo.Description = "OptioBot research: save the session's real 1-minute option bid/ask candles (read-only, no orders)."
$definition.Settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
$definition.Settings.StartWhenAvailable = $true
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.ExecutionTimeLimit = "PT60M"
$definition.Principal.UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$definition.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN; no password is stored
$definition.Principal.RunLevel = 0 # TASK_RUNLEVEL_LUA
$action = $definition.Actions.Create(0)
$action.Path = "C:\WINDOWS\System32\cmd.exe"
$action.Arguments = "/d /c `"`"$Python`" -u -m trader.cli record-options --backfill-sessions 3 >> `"$Log`" 2>&1`""
$action.WorkingDirectory = $RepoRoot
$trigger = $definition.Triggers.Create(3) # TASK_TRIGGER_WEEKLY
$trigger.DaysOfWeek = 62 # Monday through Friday
$trigger.WeeksInterval = 1
$trigger.StartBoundary = (Get-Date).Date.AddHours(15).AddMinutes(45).ToString("yyyy-MM-ddTHH:mm:ss")
$folder.RegisterTaskDefinition("OptionRecorder", $definition, 6, $null, $null, 3, $null) | Out-Null
Write-Output "Installed \OptioBot\OptionRecorder: Mon-Fri 15:45 local; log $Log"
