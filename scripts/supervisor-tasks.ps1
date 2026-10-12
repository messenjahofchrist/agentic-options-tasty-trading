[CmdletBinding()]
param(
    [ValidateSet('Install', 'Status', 'Enable', 'Disable', 'Remove')]
    [string]$Action = 'Status',
    [ValidateSet(1, 2)]
    [int]$ReviewIntervalHours = 1
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$TaskPath = '\OptioBot\'
$ComTaskPath = $TaskPath.TrimEnd([char]92)

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project Python was not found: $Python"
}

function Register-OptioTask([string]$Name, [string]$Arguments, [string]$Kind, [datetime]$Start, [int]$LimitMinutes, [int]$ReviewEveryHours = 1) {
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    try { $folder = $service.GetFolder($ComTaskPath) }
    catch {
        try { $service.GetFolder([string][char]92).CreateFolder('OptioBot', $null) | Out-Null }
        catch { }
        $folder = $service.GetFolder($ComTaskPath)
    }
    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = 'OptioBot advisory PAPER supervisory job. No broker orders are submitted.'
    $definition.Settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
    $definition.Settings.StartWhenAvailable = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.ExecutionTimeLimit = "PT${LimitMinutes}M"
    $definition.Principal.UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $definition.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN; no password is stored
    $definition.Principal.RunLevel = 0 # TASK_RUNLEVEL_LUA
    $action = $definition.Actions.Create(0) # TASK_ACTION_EXEC
    $action.Path = $Python
    $action.Arguments = $Arguments
    $action.WorkingDirectory = $RepoRoot
    if ($Kind -eq 'Daily') {
        $trigger = $definition.Triggers.Create(2) # TASK_TRIGGER_DAILY
        $trigger.DaysInterval = 1
    } else {
        $trigger = $definition.Triggers.Create(3) # TASK_TRIGGER_WEEKLY
        $trigger.DaysOfWeek = 62 # Monday through Friday
        $trigger.WeeksInterval = 1
    }
    $trigger.StartBoundary = $Start.ToString('yyyy-MM-ddTHH:mm:ss')
    if ($Kind -eq 'Hourly') { $trigger.Repetition.Interval = "PT${ReviewEveryHours}H"; $trigger.Repetition.Duration = 'PT7H' }
    if ($Kind -eq 'Watchdog') { $trigger.Repetition.Interval = 'PT1M'; $trigger.Repetition.Duration = 'PT7H' }
    $folder.RegisterTaskDefinition($Name, $definition, 6, $null, $null, 3, $null) | Out-Null
}

switch ($Action) {
    'Install' {
        $clock = Get-Date
        if ($ReviewIntervalHours -eq 2) {
            $priorSchedule = Join-Path $RepoRoot 'data\supervisor_schedule.json'
            $priorStatus = Join-Path $RepoRoot 'data\supervisor_status.json'
            if (-not (Test-Path -LiteralPath $priorSchedule) -or -not (Test-Path -LiteralPath $priorStatus)) {
                throw 'Two-hour cadence requires a completed, stable first week; hourly schedule/status history is missing.'
            }
            $scheduleRecord = Get-Content -LiteralPath $priorSchedule -Raw | ConvertFrom-Json
            $statusRecord = Get-Content -LiteralPath $priorStatus -Raw | ConvertFrom-Json
            $installed = [datetimeoffset]::Parse($scheduleRecord.installed_at_local)
            $lastSuccess = if ($statusRecord.last_successful_review_at_utc) { [datetimeoffset]::Parse($statusRecord.last_successful_review_at_utc) } else { [datetimeoffset]::MinValue }
            if (([datetimeoffset]::Now - $installed).TotalDays -lt 7 -or
                $statusRecord.last_review_status -ne 'complete' -or $statusRecord.last_error -or
                $statusRecord.last_email_status -ne 'sent' -or $statusRecord.watchdog_status -ne 'healthy' -or
                $lastSuccess -lt $installed.AddDays(7)) {
                throw 'Two-hour cadence is blocked: a full stable week of complete, emailed reviews and healthy watchdog status is not evidenced.'
            }
        }
        $service = New-Object -ComObject 'Schedule.Service'
        $service.Connect()
        try { $service.GetFolder($ComTaskPath) | Out-Null }
        catch {
            try { $service.GetFolder([string][char]92).CreateFolder('OptioBot', $null) | Out-Null }
            catch { }
        }
        $hourlyStart = $clock.Date.AddHours(8).AddMinutes(30)
        Register-OptioTask 'HourlyReview' '-m trader.supervisor review' 'Hourly' $hourlyStart 15 $ReviewIntervalHours

        $dailyStart = [datetime]::Today.AddHours(17).AddMinutes(15)
        Register-OptioTask 'DailyReport' '-m trader.supervisor daily-report' 'Daily' $dailyStart 10

        $watchdogStart = [datetime]::Today.AddHours(8).AddMinutes(25)
        Register-OptioTask 'Watchdog' '-m trader.supervisor watchdog' 'Watchdog' $watchdogStart 1
        $service = New-Object -ComObject 'Schedule.Service'
        $service.Connect()
        try { $service.GetFolder($ComTaskPath) | Out-Null }
        catch { $service.GetFolder('\').CreateFolder('OptioBot') | Out-Null }
        $nextDaily = Get-Date -Hour 17 -Minute 15 -Second 0
        if ($nextDaily -le $clock) { $nextDaily = $nextDaily.AddDays(1) }
        $nextHourly = $clock.Date.AddHours(8).AddMinutes(30)
        while ($nextHourly -le $clock -or $nextHourly.DayOfWeek -in @('Saturday', 'Sunday')) { $nextHourly = $nextHourly.AddDays(1) }
        $schedule = [ordered]@{
            active = $true
            installed_at_local = $clock.ToString('o')
            logon_type = 'Interactive'
            hourly_review = @{ interval_minutes = 60 * $ReviewIntervalHours; weekday_window_local = '08:30-15:30'; next_expected_local = $nextHourly.ToString('o') }
            daily_report = @{ local_time = '17:15'; next_expected_local = $nextDaily.ToString('o') }
            watchdog = @{ interval_minutes = 1; weekday_window_local = '08:25-15:25' }
            paper_engine = 'not_installed_pending_restart_and_position_recovery_validation'
        }
        $schedule | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $RepoRoot 'data\supervisor_schedule.json') -Encoding UTF8
        Write-Output 'Installed three advisory supervisory tasks. Engine PAPER task intentionally not installed; see docs/operations.md.'
    }
    'Status' {
        Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue |
            Where-Object { $_.TaskName -in @('HourlyReview', 'DailyReport', 'Watchdog', 'PaperEngine') } |
            ForEach-Object {
                $info = Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath
                [pscustomobject]@{ TaskName=$_.TaskName; State=$_.State; RunAs=$_.Principal.UserId;
                    LogonType=$_.Principal.LogonType; LastRunTime=$info.LastRunTime; NextRunTime=$info.NextRunTime;
                    LastTaskResult=$info.LastTaskResult }
            }
        & $Python -m trader.supervisor status
    }
    'Enable' {
        foreach ($name in @('HourlyReview', 'DailyReport', 'Watchdog')) {
            Enable-ScheduledTask -TaskName $name -TaskPath $TaskPath -ErrorAction Stop | Out-Null
        }
        $schedulePath = Join-Path $RepoRoot 'data\supervisor_schedule.json'
        if (Test-Path -LiteralPath $schedulePath) {
            $scheduleRecord = Get-Content -LiteralPath $schedulePath -Raw | ConvertFrom-Json
            $scheduleRecord.active = $true
            $scheduleRecord | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $schedulePath -Encoding UTF8
        }
        Write-Output 'Enabled the OptioBot advisory supervisory tasks.'
    }
    'Disable' {
        foreach ($name in @('HourlyReview', 'DailyReport', 'Watchdog')) {
            Disable-ScheduledTask -TaskName $name -TaskPath $TaskPath -ErrorAction SilentlyContinue | Out-Null
        }
        $schedulePath = Join-Path $RepoRoot 'data\supervisor_schedule.json'
        if (Test-Path -LiteralPath $schedulePath) {
            $scheduleRecord = Get-Content -LiteralPath $schedulePath -Raw | ConvertFrom-Json
            $scheduleRecord.active = $false
            $scheduleRecord | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $schedulePath -Encoding UTF8
        }
        Write-Output 'Disabled the OptioBot advisory supervisory tasks.'
    }
    'Remove' {
        foreach ($name in @('HourlyReview', 'DailyReport', 'Watchdog')) {
            Unregister-ScheduledTask -TaskName $name -TaskPath $TaskPath -Confirm:$false -ErrorAction SilentlyContinue
        }
        $schedulePath = Join-Path $RepoRoot 'data\supervisor_schedule.json'
        if (Test-Path -LiteralPath $schedulePath) {
            $scheduleRecord = Get-Content -LiteralPath $schedulePath -Raw | ConvertFrom-Json
            $scheduleRecord.active = $false
            $scheduleRecord.removed_at_local = (Get-Date).ToString('o')
            $scheduleRecord | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $schedulePath -Encoding UTF8
        }
        Write-Output 'Removed OptioBot advisory supervisory tasks.'
    }
}
