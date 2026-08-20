@echo off
setlocal EnableDelayedExpansion
title F^&O System — Full Launcher
echo.
echo  F^&O SYSTEM - single command (everything)
echo  ==========================================

cd /d C:\Users\yashs\trading_system
set PYTHONIOENCODING=utf-8

REM ── [1/5] Free the ports (targeted; never kills scheduled tasks) ──
echo [1/5] Freeing ports 8000/3000/8511...
for %%P in (8000 3000 8511) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| find ":%%P" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
)

REM ── [2/5] Daily ticks: allocation decision + swing screen/learner ──
echo [2/5] Allocation tick (path-#1 decision)...
python allocation_task.py
echo [3/5] Swing tick (screen + journal + resolve + learn)... may take ~2 min
python swing_screen.py --journal --json
python swing_tracker.py

REM ── [4/5] Core processes ────────────────────────────────────────
echo [4/5] Starting scanner, executor, API...
start "FO-Scanner"  cmd /k "color 0A && echo [SCANNER] && python scan_only_v2.py"
REM scan_only_v2 owns signals.json; do not start Aladdin's legacy duplicate scanner.
start "FO-Executor" cmd /k "color 0D && echo [EXECUTOR paper] && python aladdin_runner.py --no-scan"
start "FO-API"      cmd /k "color 0B && echo [API :8000] && python -m uvicorn api_server:app --port 8000"

REM ── [5/5] Both UIs ───────────────────────────────────────────────
echo [5/5] Starting UIs (Next.js :3000 + Swing Finder :8511)...
start "FO-UI-Next"   cmd /k "color 0E && echo [NEXT.JS :3000] && cd trading-ui && npm run dev"
start "FO-UI-Swing"  cmd /k "color 0C && echo [SWING FINDER :8511 - no API keys] && streamlit run swing_app.py --server.port 8511"

call :wait_for_http "http://localhost:8000/api/health" "API"
call :wait_for_http "http://localhost:3000"            "Next.js"
call :wait_for_http "http://localhost:8511"            "Swing Finder"
start http://localhost:3000
start http://localhost:8511

echo.
echo  ALL RUNNING:
echo    Terminal (Next.js)   http://localhost:3000   signals/chain/allocation
echo    Swing Finder         http://localhost:8511   candidates/trades/health
echo    API                  http://localhost:8000
echo  Stop everything: stop_trading.bat
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
