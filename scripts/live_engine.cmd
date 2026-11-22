@echo off
rem REAL-MONEY LIVE engine, started each weekday at 08:10 CT by the \OptioBot\LiveEngine scheduled task.
rem Owner order 2026-11-18: "It should trade daily without my intervention unless I say stop."
rem   on:  powershell -ExecutionPolicy Bypass -File scripts\live_on.ps1
rem   off: powershell -ExecutionPolicy Bypass -File scripts\live_off.ps1
rem The three LIVE settings below apply to this window only. They are never written to .env, which the PAPER
rem engines also read. live-session --unattended still checks the owner standing order (live_autostart.json),
rem the authorize-live record for the active V2 hash, the account and the kill switch before any order.
cd /d "%~dp0.."
set "TRADING_MODE=LIVE"
set "ENABLE_LIVE_TRADING=true"
set "LIVE_START_CONFIRMATION=I UNDERSTAND LIVE TRADING USES REAL MONEY"
if not exist "data\live" mkdir "data\live"
".venv\Scripts\python.exe" -u -m trader.cli live-session --strategy v2 --account-profile small --unattended >> "data\live\live_engine.log" 2>&1
