@echo off
REM ============================================================
REM  STOP.bat — the ONE way to stop this project's servers.
REM  Kills by SCRIPT FINGERPRINT (not by port), so orphaned
REM  children die too and OTHER projects' apps are never touched.
REM ============================================================
setlocal EnableDelayedExpansion
echo Stopping F^&O system processes (this project only)...

REM Kill any python running THIS project's scripts, by command-line match.
for %%S in (swing_app.py api_server scan_only_v2.py aladdin_runner.py) do (
    for /f "tokens=2 delims==" %%P in ('wmic process where "name='python.exe' and CommandLine like '%%%%S%%'" get ProcessId /format:value 2^>nul ^| findstr /R "[0-9]"') do (
        taskkill /PID %%P /F /T >nul 2>&1 && echo   killed %%S ^(PID %%P^)
    )
)

REM Kill node/next dev servers launched from THIS project's trading-ui.
for /f "tokens=2 delims==" %%P in ('wmic process where "name='node.exe' and CommandLine like '%%trading-ui%%'" get ProcessId /format:value 2^>nul ^| findstr /R "[0-9]"') do (
    taskkill /PID %%P /F /T >nul 2>&1 && echo   killed next/node ^(PID %%P^)
)

echo Done. (Other projects' servers, e.g. a Streamlit on 8501, are untouched.)
endlocal
