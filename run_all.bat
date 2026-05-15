@echo off
setlocal EnableDelayedExpansion
title F^&O Terminal — Single Command Launcher
echo.
echo  F^&O Signal Terminal
echo  =====================
echo.

REM ── Kill stale processes ─────────────────────────────────────────
echo [1/4] Killing stale processes...
call stop_trading.bat >nul 2>&1
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM streamlit.exe /T >nul 2>&1
taskkill /F /IM node.exe /T >nul 2>&1
timeout /t 2 /nobreak >nul

REM ── Token sanity ─────────────────────────────────────────────────
if not exist dhan_token.txt (
    echo [ERROR] dhan_token.txt missing. Refresh JWT from web.dhan.co.
    pause
    exit /b 1
)
for %%I in (dhan_token.txt) do echo [2/4] dhan_token.txt last mod: %%~tI

REM ── Reset learned weights cleanly ────────────────────────────────
echo [3/4] Resetting pattern weights to neutral 1.0...
python -m core.adaptive_learner --reset-direction-weights all

REM ── Launch ALADDIN (everything) + UI ─────────────────────────────
echo [4/4] Launching Aladdin (scanner+executor+tracker+API) and Streamlit UI...
start "FO-Aladdin"  cmd /k "color 0A && echo [ALADDIN — does everything] && python aladdin_runner.py"
start "FO-UI"       cmd /k "color 0E && echo [STREAMLIT :8501] && streamlit run streamlit_app.py"

echo  Waiting for services...
call :wait_for_http "http://localhost:8000/api/health" "API"
call :wait_for_http "http://localhost:8501"            "Streamlit"
start http://localhost:8501

echo.
echo  Running. Two windows:
echo    FO-Aladdin  — scanner + executor + tracker + API (port 8000)
echo    FO-UI       — Streamlit dashboard (port 8501)
echo.
echo  Stop everything: stop_trading.bat
echo.
goto :eof

:wait_for_http
set "url=%~1"
set "name=%~2"
set /a attempts=0
:wait_loop
set /a attempts+=1
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing '%url%' -TimeoutSec 2; if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { exit 0 } } catch { } exit 1" >nul 2>&1
if !errorlevel! equ 0 ( echo  !name! ready. & goto :eof )
if !attempts! geq 30 ( echo  !name! slow start, continuing. & goto :eof )
timeout /t 2 /nobreak >nul
goto wait_loop
