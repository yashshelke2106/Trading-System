@echo off
REM run_capture.bat - daily market-capture job, invoked by Windows Task
REM Scheduler (task: TradingSystem_MarketCapture, weekdays 16:15 IST).
REM Appends to logs\capture_scheduler.log so silent failures are visible -
REM this repo's history is capture layers dying quietly; the log is the pulse.
cd /d C:\Users\yashs\trading_system
echo. >> logs\capture_scheduler.log
echo ===== capture run %date% %time% ===== >> logs\capture_scheduler.log
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe capture_task.py >> logs\capture_scheduler.log 2>&1
) else (
    python capture_task.py >> logs\capture_scheduler.log 2>&1
)
echo ===== exit %errorlevel% ===== >> logs\capture_scheduler.log
