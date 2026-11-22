# Recreates every \OptioBot\ scheduled task on this PC from scripts\tasks\*.xml (made by export_tasks.ps1).
#
# New-PC checklist (in this order):
#   1. git clone the repo; install Python 3.14 and Git; then in the repo folder:
#        py -3.14 -m venv .venv ; .venv\Scripts\python.exe -m pip install -e .
#   2. Copy .env from the old PC by hand (USB stick or password manager). Never through GitHub or email.
#   3. Copy the data\ folder from the old PC if you want to keep the paper ledgers, scoreboards, recorded option
#      prices and advisor history. Without it every paper account starts fresh.
#   4. Install and sign in to the Claude Code and Codex CLIs (the advisor and the hourly reviewers call them).
#   5. Windows: time zone Central, "Set time automatically" on, sleep never (the PC must stay awake 8:00-15:30 CT).
#   6. Run this script. Tasks are installed DISABLED unless you add -Enable, so two PCs never trade at once.
#   7. Turn the old PC's tasks off (Disable-ScheduledTask) before enabling these.
#   8. LIVE is not installed by this script. Turn it off on the old PC first (scripts\live_off.ps1), then on the
#      new PC run authorize-live again and scripts\live_on.ps1 (docs/live_trading.md section 14). Never run LIVE
#      on two PCs.
#   9. Check: .venv\Scripts\python.exe -m pytest tests -q   and   python -m trader.cli live-check --strategy v2 --account-profile small
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1            # install, all disabled
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Enable    # install and enable
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -DryRun    # show what it would do
param([switch]$Enable, [switch]$DryRun, [string]$TaskPath = '\OptioBot\')
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$TaskDir = Join-Path $PSScriptRoot 'tasks'
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value

if (-not (Test-Path $Python)) { throw "No virtual environment at $Python. Do step 1 first (see the top of this script)." }
if (-not (Test-Path (Join-Path $RepoRoot '.env'))) { Write-Warning "No .env in $RepoRoot. The engines cannot log in to tastytrade until you copy it (step 2)." }
$files = Get-ChildItem $TaskDir -Filter *.xml
if (-not $files) { throw "No task files in $TaskDir. Run export_tasks.ps1 on the old PC and commit scripts\tasks." }

# Folders the tasks write their logs into (cmd's >> fails when the folder is missing).
foreach ($d in 'data', 'data\profiles\small_account_paper', 'data\option_history', 'data\in_play', 'data\v29\original_paper',
               'data\v29\small_account_paper', 'data\live', 'data\advisor') {
    if (-not $DryRun) { New-Item -ItemType Directory -Force (Join-Path $RepoRoot $d) | Out-Null }
}

foreach ($file in $files) {
    $name = $file.BaseName
    $xml = [System.IO.File]::ReadAllText($file.FullName)
    $xml = $xml.Replace('{REPO}', $RepoRoot).Replace('{USER_SID}', $Sid)
    # Enabled or not is written into the task itself, so a task is never briefly live while being installed.
    $xml = [regex]::Replace($xml, '\s*<Enabled>[^<]*</Enabled>', '')
    $xml = $xml -replace '<Settings>', "<Settings>`r`n    <Enabled>$(if ($Enable) { 'true' } else { 'false' })</Enabled>"
    if ($DryRun) { "would install $TaskPath$name ($(if ($Enable) { 'enabled' } else { 'disabled' }))"; continue }
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $name -Xml $xml -Force | Out-Null
}
if (-not $DryRun) {
    Get-ScheduledTask -TaskPath $TaskPath | ForEach-Object {
        [pscustomobject]@{Task = $_.TaskName; State = $_.State; Next = ($_ | Get-ScheduledTaskInfo).NextRunTime} } | Format-Table -AutoSize
    if (-not $Enable) { "Installed DISABLED. When the old PC is off: powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Enable" }
}
