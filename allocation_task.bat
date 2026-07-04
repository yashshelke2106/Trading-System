@echo off
REM Path-#1 allocation autopilot tick — registered in Windows Task Scheduler.
REM Refreshes NIFTY data, recomputes the target allocation, writes
REM logs\allocation_state.json and alerts on a 200-DMA state flip.
cd /d C:\Users\yashs\trading_system
set PYTHONIOENCODING=utf-8
python allocation_task.py >> logs\allocation_task.log 2>&1
