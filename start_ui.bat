@echo off
setlocal EnableDelayedExpansion
title F^&O UI Launcher
echo.
echo  UI ONLY - Next.js terminal + Streamlit Swing Finder
echo  ====================================================

cd /d C:\Users\yashs\trading_system
set PYTHONIOENCODING=utf-8

REM free UI/API ports (targeted)
for %%P in (8000 3000 8511) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| find ":%%P" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
)

REM API backend (Next.js terminal needs it; no Dhan token required to serve)
start "FO-API"      cmd /k "color 0B && echo [API :8000] && python -m uvicorn api_server:app --port 8000"
REM Next.js terminal
start "FO-UI-Next"  cmd /k "color 0E && echo [NEXT.JS :3000] && cd trading-ui && npm run dev"
REM Streamlit Swing Finder (no API keys at all)
start "FO-UI-Swing" cmd /k "color 0C && echo [SWING FINDER :8511] && streamlit run swing_app.py --server.port 8511"

call :wait_for_http "http://localhost:3000" "Next.js"
call :wait_for_http "http://localhost:8511" "Swing Finder"
start http://localhost:3000
start http://localhost:8511

echo.
echo  UIs running:  :3000 (terminal)   :8511 (swing finder)
echo  Stop: stop_trading.bat
goto :eof

:wait_for_http
set "url=%~1"
set "name=%~2"
set /a attempts=0
:wait_loop
set /a attempts+=1
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing '%url%' -TimeoutSec 2; if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { exit 0 } } catch { } exit 1" >nul 2>&1
if !errorlevel! equ 0 ( echo  !name! ready. & goto :eof )
if !attempts! geq 45 ( echo  !name! slow start, continuing. & goto :eof )
timeout /t 2 /nobreak >nul
goto wait_loop
