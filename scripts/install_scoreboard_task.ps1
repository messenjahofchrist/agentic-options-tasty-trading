# Install (or remove) the nightly scoreboard: \OptioBot\Scoreboard, weekdays 16:40 local (after the option recorder
# has saved the day's quotes at 15:45). Each run: python -m trader.scoreboard run. It rebuilds the option cost table
# (data\option_cost_table.json), scores the shorter-dated contract trial, the cost gate, and the advisor's ideas,
# vetoes and watchlist on real prices, writes reports\scoreboard\latest.md and emails it. It only reads ledgers and
# logs; it never places orders or changes the bot (owner request 2026-11-11).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_scoreboard_task.ps1            # install / update
#   powershell -ExecutionPolicy Bypass -File scripts\install_scoreboard_task.ps1 -Remove    # remove
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw "Project Python was not found: $Python" }

$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder('\OptioBot')
if ($Remove) {
    $folder.DeleteTask('Scoreboard', 0)
    Write-Output 'Removed \OptioBot\Scoreboard'
    return
}
$definition = $service.NewTask(0)
$definition.RegistrationInfo.Description = 'OptioBot nightly scoreboard (PAPER). Reads ledgers, logs and recorded quotes; writes reports. No orders.'
$definition.Settings.MultipleInstances = 2          # ignore a new run while one is still going
$definition.Settings.StartWhenAvailable = $true     # a missed evening runs when the machine is next available
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.ExecutionTimeLimit = 'PT1H'
$definition.Settings.Priority = 7                   # below normal: never competes with anything else
$definition.Principal.UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$definition.Principal.LogonType = 3                 # interactive token; no password stored (same as HourlyReview)
$definition.Principal.RunLevel = 0
$action = $definition.Actions.Create(0)
$action.Path = $Python
$action.Arguments = '-m trader.scoreboard run'
$action.WorkingDirectory = $RepoRoot
$trigger = $definition.Triggers.Create(3)           # weekly
$trigger.DaysOfWeek = 62                            # Monday-Friday
$trigger.WeeksInterval = 1
$trigger.StartBoundary = (Get-Date).Date.AddHours(16).AddMinutes(40).ToString('yyyy-MM-ddTHH:mm:ss')
$folder.RegisterTaskDefinition('Scoreboard', $definition, 6, $null, $null, 3, $null) | Out-Null
$task = Get-ScheduledTask -TaskPath '\OptioBot\' -TaskName 'Scoreboard'
Write-Output "Installed \OptioBot\Scoreboard; next run $((Get-ScheduledTaskInfo $task).NextRunTime)"
