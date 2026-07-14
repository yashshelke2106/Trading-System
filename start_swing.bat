@echo off
setlocal EnableDelayedExpansion
REM No-API swing finder — zero credentials (free yfinance data).
REM DEDICATED PORT 8511: 8501 is Streamlit's global default, so OTHER
REM projects' dashboards land there — this project stays off it entirely.
REM GUARD: if 8511 is listening, check the owning PROCESS is running
REM swing_app.py (commandline fingerprint) before reusing; a stranger
REM on the port -> fall over to 8512.
cd /d C:\Users\yashs\trading_system
set PYTHONIOENCODING=utf-8
set PORT=8511

set "OWNPID="
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /C:":%PORT%" ^| findstr /C:"LISTENING"') do set "OWNPID=%%a"
if not defined OWNPID goto launch

wmic process where "ProcessId=%OWNPID%" get CommandLine 2>nul | findstr /C:"swing_app.py" >nul
if not errorlevel 1 (
    echo Swing Finder already running on %PORT% -- opening browser.
    start http://localhost:%PORT%
    exit /b 0
)
echo Port %PORT% is held by ANOTHER app ^(PID %OWNPID%^) -- starting on 8512 instead.
set PORT=8512

:launch
echo Starting Swing Finder on http://localhost:%PORT% (no API keys required)...
streamlit run swing_app.py --server.port %PORT%
