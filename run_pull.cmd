@echo off
rem ---------------------------------------------------------------------------
rem mt-monitor scheduled pull wrapper.
rem Writes one start line and one end line (with exit code) per run into a
rem per-day log, so every scheduled minute can be traced afterwards:
rem     logs\pull-YYYY-MM-DD.log
rem Audit the result with:  .venv\Scripts\python.exe -m src.mt_monitor.cli audit
rem Keep this file CRLF and ASCII-only: cmd.exe mis-parses LF-only batch files
rem and garbles non-ASCII bytes in the OEM codepage.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
if not exist "logs" mkdir "logs"

rem %date% on this machine looks like: <weekday> 2026/09/14  -> tokens 2,3,4
for /f "tokens=2,3,4 delims=/- " %%a in ("%date%") do set "DAY=%%a-%%b-%%c"
set "LOG=logs\pull-%DAY%.log"

set "T0=%time:~0,8%"
echo [%DAY% %T0%] --- pull start --- >> "%LOG%"
".venv\Scripts\python.exe" -m src.mt_monitor.cli pull >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
set "T1=%time:~0,8%"
echo [%DAY% %T1%] --- pull end (exit=%RC%) --- >> "%LOG%"
endlocal
