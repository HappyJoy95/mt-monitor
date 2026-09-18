@echo off
rem ---------------------------------------------------------------------------
rem mt-monitor scheduled pull runner (Task Scheduler task "MT Monitor").
rem Logging happens inside Python (cli pull-logged) instead of here: cmd's ">>"
rem redirection is not shareable, so a run overlapping a long one could not open
rem the shared log and silently did nothing (observed 2026-09-18 19:55). Python's
rem append mode tolerates concurrent writers, so every attempt lands in
rem logs\pull-YYYY-MM-DD.log. Audit with: cli audit [--date YYYY-MM-DD].
rem Keep this file CRLF and ASCII-only: cmd.exe mis-parses LF-only batch files
rem and garbles non-ASCII bytes in the OEM codepage.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
".venv\Scripts\python.exe" -m src.mt_monitor.cli pull-logged
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
