@echo off
REM ===========================================================
REM  Dhan preflight — double-click this file. No typing needed.
REM  It checks that Dhan market data is flowing, prints a
REM  PASS/FAIL report, and (on PASS) offers to run the backtest.
REM  The window stays open so you can read + copy the output.
REM ===========================================================
title Dhan Preflight Check
cd /d "%~dp0"

echo.
echo  Running Dhan data check... (10-20 seconds)
echo  ----------------------------------------------------------
echo.

REM Try py launcher first, fall back to python
where py >nul 2>&1
if %errorlevel%==0 (
    py verify_dhan.py
) else (
    python verify_dhan.py
)

set PREFLIGHT_CODE=%errorlevel%
echo.
echo  ----------------------------------------------------------

if "%PREFLIGHT_CODE%"=="0" (
    echo  PREFLIGHT PASSED. Data is flowing.
    echo.
    set /p RUNBT="  Run the backtest now to check the edge? [Y/N]: "
    if /I "%RUNBT%"=="Y" (
        echo.
        echo  Running backtest on real Dhan data... (1-3 min)
        echo.
        set ONLY_LONG=1
        set PRECISION_MODE=0
        set DISABLE_G9=1
        set DISABLE_G10=1
        set DISABLE_REGIME_GATE=1
        where py >nul 2>&1
        if %errorlevel%==0 ( py backtest_india_swing.py ) else ( python backtest_india_swing.py )
        echo.
        echo  Backtest done — read the RESULTS block above ^(Profit factor / Total trades^).
    )
) else (
    echo  PREFLIGHT FAILED. Dhan data not flowing — see hints above.
)

echo.
echo  ==========================================================
echo   Copy everything above this line and paste it back.
echo  ==========================================================
echo.
pause
