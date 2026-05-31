@echo off
setlocal EnableDelayedExpansion
title F&O Pullback (single-setup) Launcher
echo.
echo  F^&O PULLBACK — single-setup mode
echo  =================================
echo  Strategy: trend pullback continuation, LONGS ONLY, regime-gated.
echo  Backtest edge: PF 1.17, +0.10R (longs-only vs 0.94 mixed).
echo  Shorts OFF (12%% WR). A-grade noisy — B-grade carries the edge.
echo.

REM ── Proven config (env overrides) ──────────────────────────────────
REM ONLY_LONG=1        : kill all shorts (NSE stocks grind up, crash rare)
REM PRECISION_MODE=0   : permissive trend gate (more trades; v3 too tight = 5/2yr)
REM Regime gate ON     : DISABLE_REGIME_GATE unset -> india_swing skips scan
REM                      when NIFTY ADX<20 (chop) or earnings cluster.
REM G9/G10 LIVE         : sector-leader + ML stay ON in live (only disabled
REM                      for backtest apples-to-apples).
set ONLY_LONG=1
set PRECISION_MODE=0
set PAPER_TRADE=1
REM Instrument: stock FUTURES, not options. 16-day journal proof — on SL hits
REM spot moved only -0.6%% but option premium lost -9.56%% (theta/IV/spread).
REM Futures carry the spot edge (PF 1.17) without the premium-decay tax.
set INSTRUMENT_MODE=futures

REM Kill stale ports
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8000" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":3000" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1

REM Window 1: Scanner (pullback longs-only) — passes env through
start "FO-Pullback-Scanner" cmd /k "color 0A && set ONLY_LONG=1 && set PRECISION_MODE=0 && echo [PULLBACK SCANNER] longs-only, regime-gated && python scan_only_v2.py"

REM Window 2: Signal outcome tracker (writes journal + daily metrics at close)
start "FO-Tracker" cmd /k "color 0D && echo [TRACKER] logging outcomes -^> signal_journal.jsonl && python signal_tracker.py"

REM Window 3: FastAPI backend
start "FO-API" cmd /k "color 0B && echo [API] http://localhost:8000 && python -m uvicorn api_server:app --reload --port 8000"

REM Window 4: Next.js UI
start "FO-UI" cmd /k "color 0E && echo [UI] http://localhost:3000 && cd trading-ui && npm run dev"

echo  Waiting for API and UI...
call :wait_for_http "http://localhost:8000/api/health" "API"
call :wait_for_http "http://localhost:3000" "UI"
start http://localhost:3000

echo.
echo  PULLBACK mode running. PAPER_TRADE locked ON.
echo  Scanner  ^> FO-Pullback-Scanner
echo  Tracker  ^> FO-Tracker (journals every signal, metrics at 15:31)
echo  UI       ^> http://localhost:3000
echo.
echo  FORWARD-TEST PROTOCOL: run daily 4 weeks. Do NOT tune mid-test.
echo  Check logs/metrics_daily.jsonl for drift_alert. Kill if 30d PF^<1.0
echo  after >=30 trades. Promote if PF^>1.2.
echo.
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
if !attempts! geq 30 ( echo  !name! slow, continuing. & goto :eof )
timeout /t 2 /nobreak >nul
goto wait_loop
