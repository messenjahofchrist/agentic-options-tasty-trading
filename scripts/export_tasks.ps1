# Saves every \OptioBot\ scheduled task as XML in scripts\tasks\, so another PC can recreate them exactly
# (scripts\install_tasks.ps1). Read-only for the tasks themselves.
#
# The saved files are made portable and private:
#   - this checkout's folder becomes {REPO}   (install_tasks.ps1 puts the new PC's folder back in)
#   - the Windows account id (SID) becomes {USER_SID}   (install_tasks.ps1 uses whoever installs them)
#
#   powershell -ExecutionPolicy Bypass -File scripts\export_tasks.ps1
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$OutDir = Join-Path $PSScriptRoot 'tasks'
New-Item -ItemType Directory -Force $OutDir | Out-Null
Get-ChildItem $OutDir -Filter *.xml | Remove-Item -Force

# LiveEngine (real money) is never exported: a new PC turns LIVE on only with scripts\live_on.ps1.
$tasks = Get-ScheduledTask -TaskPath '\OptioBot\' | Where-Object { $_.TaskName -ne 'LiveEngine' }
foreach ($task in $tasks) {
    $xml = Export-ScheduledTask -TaskPath '\OptioBot\' -TaskName $task.TaskName
    # the repo folder, whatever its capitalisation in each task
    $xml = [regex]::Replace($xml, [regex]::Escape($RepoRoot), '{REPO}', 'IgnoreCase')
    $xml = [regex]::Replace($xml, '<UserId>[^<]*</UserId>', '<UserId>{USER_SID}</UserId>')
    $xml = $xml -replace 'encoding="UTF-16"', 'encoding="UTF-8"'
    $state = if ($task.State -eq 'Disabled') { 'disabled' } else { 'enabled' }
    $xml = $xml -replace '(<Task [^>]*>)', "`$1`r`n  <!-- OptioBot export: $state on the source PC -->"
    [System.IO.File]::WriteAllText((Join-Path $OutDir "$($task.TaskName).xml"), $xml, (New-Object System.Text.UTF8Encoding($false)))
}
"Exported $($tasks.Count) task(s) to $OutDir"
$leak = Select-String -Path (Join-Path $OutDir '*.xml') -Pattern 'S-1-5-21|\\Users\\' -List
if ($leak) { Write-Warning "Check these files before committing: $($leak.Path -join ', ')" }
