@echo off
setlocal EnableDelayedExpansion
title F&O Terminal Launcher
echo.
echo  Starting F^&O Signal Terminal...
echo  ================================
echo.

REM Kill any existing instances on these ports
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8000" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":3000" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1

REM Window 1: Scanner
start "FO-Scanner" cmd /k "color 0A && echo [SCANNER] Starting... && python scan_only_v2.py"

REM Window 2: Paper-trading executor
start "FO-Executor" cmd /k "color 0D && echo [EXECUTOR] Starting... && python aladdin_runner.py"

REM Window 3: FastAPI backend
REM no --reload: the reloader parent/child pair wedges on Windows (zombie
REM holds :8000 answering nothing) and killed children leave orphans.
start "FO-API" cmd /k "color 0B && echo [API] Starting on http://localhost:8000 && python -m uvicorn api_server:app --port 8000"

REM Window 4: Next.js UI
start "FO-UI" cmd /k "color 0E && echo [UI] Starting on http://localhost:3000 && cd trading-ui && npm run dev"

echo  Waiting for API and UI to boot...
call :wait_for_http "http://localhost:8000/api/health" "API"
call :wait_for_http "http://localhost:3000" "UI"

echo  Opening browser...
start http://localhost:3000

echo.
echo  All services started.
echo  Scanner   ^> FO-Scanner window
echo  Executor  ^> FO-Executor window
echo  API       ^> http://localhost:8000
echo  UI        ^> http://localhost:3000
echo.
echo  Run stop_trading.bat to shut down.
goto :eof

:wait_for_http
set "url=%~1"
set "name=%~2"
set /a attempts=0

:wait_loop
set /a attempts+=1
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing '%url%' -TimeoutSec 2; if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { exit 0 } } catch { } exit 1" >nul 2>&1
if !errorlevel! equ 0 (
    echo  !name! ready.
    goto :eof
)
if !attempts! geq 30 (
    echo  !name! did not respond in time. Continuing anyway.
    goto :eof
)
timeout /t 2 /nobreak >nul
goto wait_loop
