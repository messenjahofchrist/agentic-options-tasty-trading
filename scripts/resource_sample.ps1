# One sample of what the bot's processes use (owner, 2026-11-12: "optimize both programs ... memory and CPU").
# Run every 5 minutes in market hours by \OptioBot\ResourceMonitor; appends one JSON line per process to
# data\resource_usage.jsonl. Read-only: it never stops or changes a process.
$ErrorActionPreference = 'SilentlyContinue'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$out = Join-Path $RepoRoot 'data\resource_usage.jsonl'
$stamp = (Get-Date).ToUniversalTime().ToString('o')
$os = Get-CimInstance Win32_OperatingSystem
$cpus = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
$lines = @()
$lines += (@{ts = $stamp; kind = 'machine'; free_mb = [math]::Round($os.FreePhysicalMemory / 1KB); total_mb = [math]::Round($os.TotalVisibleMemorySize / 1KB); logical_cpus = $cpus} | ConvertTo-Json -Compress)
foreach ($p in Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='node.exe' OR Name='codex.exe' OR Name='claude.exe' OR Name='gemini.exe'") {
    $cmd = [string]$p.CommandLine
    $role = 'other'
    if ($cmd -match 'side-session --ledger (\w+)') { $role = "v29_engine_$($Matches[1])" }
    elseif ($cmd -match 'paper-session.*--account-profile small') { $role = 'v28_engine_1k' }
    elseif ($cmd -match 'paper-session') { $role = 'v28_engine_10k' }
    elseif ($cmd -match 'record-options') { $role = 'option_recorder' }
    elseif ($cmd -match 'supervisor (\w+)') { $role = "supervisor_$($Matches[1])" }
    elseif ($cmd -match 'trader\.advisor') { $role = 'advisor' }
    elseif ($cmd -match 'profile-report|side-report|spreads-run') { $role = ($Matches[0]) }
    elseif ($p.Name -in @('node.exe', 'codex.exe', 'claude.exe', 'gemini.exe')) { $role = "ai_cli_$($p.Name)" }
    $proc = Get-Process -Id $p.ProcessId
    if (-not $proc) { continue }
    $lines += (@{ts = $stamp; kind = 'process'; pid = $p.ProcessId; parent = $p.ParentProcessId; name = $p.Name; role = $role
                 working_set_mb = [math]::Round($proc.WorkingSet64 / 1MB, 1); private_mb = [math]::Round($proc.PrivateMemorySize64 / 1MB, 1)
                 cpu_seconds = [math]::Round($proc.TotalProcessorTime.TotalSeconds, 1)
                 started = $proc.StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress)
}
Add-Content -Path $out -Value $lines -Encoding utf8
