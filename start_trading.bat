@echo off
setlocal EnableDelayedExpansion
title F&O Terminal Launcher
echo.
echo  Starting F^&O Signal Terminal...
echo  ================================
echo.

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

REM Free THIS project's ports only. 8501 is deliberately absent: it is
REM Streamlit's global default and belongs to other projects' dashboards.
for %%P in (8000 3000 8511 8513 8503) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| find ":%%P" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
)

REM Window 1: Scanner
start "FO-Scanner" cmd /k "color 0A && echo [SCANNER] Starting... && python scan_only_v2.py"

REM Window 2: Paper-trading executor
REM scan_only_v2 owns signals.json; do not start Aladdin's legacy duplicate scanner.
start "FO-Executor" cmd /k "color 0D && echo [EXECUTOR] Starting... && python aladdin_runner.py --no-scan"

REM Window 3: FastAPI backend
REM no --reload: the reloader parent/child pair wedges on Windows (zombie
REM holds :8000 answering nothing) and killed children leave orphans.
start "FO-API" cmd /k "color 0B && echo [API] Starting on http://localhost:8000 && python -m uvicorn api_server:app --port 8000"

REM Window 4: Next.js UI
REM Purge the Turbopack cache before every launch.
REM
REM Symptom it fixes: the dashboard serves the header/nav but EVERY route
REM returns 404 ("This page could not be found"). Seen three times now; a
REM .next.stale.* directory from an earlier occurrence is still in the repo.
REM Cause: .next is left holding only dev\ with no route manifests, which
REM happens when the dev server is force-killed mid-compile (stop_trading.bat
REM uses taskkill /F) or when two servers race the same cache directory.
REM A cold Turbopack build costs ~1.3s, so buying determinism here is free.
if exist "trading-ui\.next" rmdir /s /q "trading-ui\.next"
start "FO-UI" cmd /k "color 0E && echo [UI] Starting on http://localhost:3000 && cd trading-ui && npm run dev"

REM ── Streamlit dashboards, each pinned to its own port ──────────────────────
REM Started, but the browser is NOT opened on them (only :3000 is). The legacy
REM terminal was deleted in Jul 2026 because it kept surfacing unannounced and
REM reading as "the old dashboard"; listed here on a fixed port, it cannot
REM surprise anyone.
start "FO-Swing"  cmd /k "color 0C && echo [SWING FINDER :8511 - no API keys] && python -m streamlit run swing_app.py --server.port 8511"
start "FO-Legacy" cmd /k "color 09 && echo [LEGACY TERMINAL :8513 - needs Dhan token] && python -m streamlit run streamlit_app.py --server.port 8513"
start "FO-Events" cmd /k "color 0F && echo [EVENTS :8503 - read-only] && python -m streamlit run events_app.py --server.port 8503"

echo  Waiting for services to boot...
call :wait_for_http "http://localhost:8000/api/health" "API"
call :wait_for_http "http://localhost:3000" "UI"
call :wait_for_http "http://localhost:8511" "Swing Finder"
call :wait_for_http "http://localhost:8513" "Legacy terminal"
call :wait_for_http "http://localhost:8503" "Events"

echo  Opening browser...
start http://localhost:3000

echo.
echo  All services started.
echo  Scanner    ^> FO-Scanner window
echo  Executor   ^> FO-Executor window
echo  API        ^> http://localhost:8000
echo.
echo  Dashboards:
echo    Next.js terminal ^(main^)   http://localhost:3000
echo    Swing Finder ^(no keys^)    http://localhost:8511
echo    Legacy terminal ^(Dhan^)    http://localhost:8513
echo    Events / news             http://localhost:8503
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
