@echo off
rem ---------------------------------------------------------------------------
rem mt-monitor debugging-browser watchdog runner (scheduled task "MT Edge Watch").
rem Probes CDP on port 9222; after N consecutive failures the watchdog restarts
rem only the Edge instance bound to the monitor profile and alerts WeChat.
rem Output goes to logs\edge-watch-YYYY-MM-DD.log (one line per check).
rem Keep this file CRLF and ASCII-only: cmd.exe mis-parses LF-only batch files.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
if not exist "logs" mkdir "logs"

rem %date% on this machine looks like: <weekday> 2026/09/17  -> tokens 2,3,4
for /f "tokens=2,3,4 delims=/- " %%a in ("%date%") do set "DAY=%%a-%%b-%%c"
set "LOG=logs\edge-watch-%DAY%.log"

".venv\Scripts\python.exe" -m src.mt_monitor.cli edge-watch >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
