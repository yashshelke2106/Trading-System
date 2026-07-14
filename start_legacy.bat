@echo off
setlocal EnableDelayedExpansion
REM Legacy Streamlit dashboard (needs Dhan token for live F&O data).
REM DEDICATED PORT 8513 — never collides with Swing Finder (8511) or
REM other projects' Streamlit apps (8501 default). Same process-
REM fingerprint guard as start_swing.bat.
cd /d C:\Users\yashs\trading_system
set PYTHONIOENCODING=utf-8
set PORT=8513

set "OWNPID="
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /C:":%PORT%" ^| findstr /C:"LISTENING"') do set "OWNPID=%%a"
if not defined OWNPID goto launch

wmic process where "ProcessId=%OWNPID%" get CommandLine 2>nul | findstr /C:"streamlit_app.py" >nul
if not errorlevel 1 (
    echo Legacy dashboard already running on %PORT% -- opening browser.
    start http://localhost:%PORT%
    exit /b 0
)
echo Port %PORT% is held by ANOTHER app ^(PID %OWNPID%^) -- starting on 8514 instead.
set PORT=8514

:launch
echo Starting LEGACY dashboard on http://localhost:%PORT% ...
streamlit run streamlit_app.py --server.port %PORT%
