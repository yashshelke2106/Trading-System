@echo off
REM run_capture.bat - daily market-capture job, invoked by Windows Task
REM Scheduler (task: TradingSystem_MarketCapture, weekdays 16:15 IST).
REM Appends to logs\capture_scheduler.log so silent failures are visible -
REM this repo's history is capture layers dying quietly; the log is the pulse.
cd /d C:\Users\yashs\trading_system
echo. >> logs\capture_scheduler.log
echo ===== capture run %date% %time% ===== >> logs\capture_scheduler.log
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
%PY% capture_task.py >> logs\capture_scheduler.log 2>&1
echo ===== capture exit %errorlevel% ===== >> logs\capture_scheduler.log

REM Daily premium HARVEST: mark the paper book + report holds/actions. This is
REM the operational system now (beta + defined-risk VRP). It does NOT scan for
REM edge — that is deliberate. Output to its own log so the harvest pulse is
REM separate from the capture pulse.
echo ===== harvest run %date% %time% ===== >> logs\harvest.log
%PY% harvest.py --capital 100000 >> logs\harvest.log 2>&1
echo ===== harvest exit %errorlevel% ===== >> logs\harvest.log
