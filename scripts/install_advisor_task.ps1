# Install (or remove) the read-only market-news advisor: \OptioBot\Advisor, weekdays, 08:00 local (09:00 ET, before
# the open) and then hourly through 14:00 local (15:00 ET). Each run: python -m trader.advisor run (web search only,
# emails a summary, writes data\advisor\). The agent that does the work is set in agents.json (role "advisor").
# It never places orders. Its symbol vetoes (data\advisor\vetoes.json) are read by the engines when V2's ai_veto stage
# is on; its watchlist and trade ideas are only logged and scored (owner requests 2026-11-10 and 2026-11-11).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_advisor_task.ps1            # install / update
#   powershell -ExecutionPolicy Bypass -File scripts\install_advisor_task.ps1 -Remove    # remove
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw "Project Python was not found: $Python" }

$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder('\OptioBot')
if ($Remove) {
    $folder.DeleteTask('Advisor', 0)
    Write-Output 'Removed \OptioBot\Advisor'
    return
}
$definition = $service.NewTask(0)
$definition.RegistrationInfo.Description = 'OptioBot read-only market-news advisor (PAPER). No orders; nothing the engines read.'
$definition.Settings.MultipleInstances = 2          # ignore a new run while one is still going
$definition.Settings.StartWhenAvailable = $false    # a missed hour is skipped, not run late
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.ExecutionTimeLimit = 'PT15M'
$definition.Principal.UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$definition.Principal.LogonType = 3                 # interactive token; no password stored (same as HourlyReview)
$definition.Principal.RunLevel = 0
$action = $definition.Actions.Create(0)
$action.Path = $Python
$action.Arguments = '-m trader.advisor run'
$action.WorkingDirectory = $RepoRoot
$trigger = $definition.Triggers.Create(3)           # weekly
$trigger.DaysOfWeek = 62                            # Monday-Friday
$trigger.WeeksInterval = 1
$trigger.StartBoundary = (Get-Date).Date.AddHours(8).ToString('yyyy-MM-ddTHH:mm:ss')
$trigger.Repetition.Interval = 'PT1H'
$trigger.Repetition.Duration = 'PT6H5M'             # 08:00 ... 14:00 local
$folder.RegisterTaskDefinition('Advisor', $definition, 6, $null, $null, 3, $null) | Out-Null
$task = Get-ScheduledTask -TaskPath '\OptioBot\' -TaskName 'Advisor'
Write-Output "Installed \OptioBot\Advisor; next run $((Get-ScheduledTaskInfo $task).NextRunTime)"
