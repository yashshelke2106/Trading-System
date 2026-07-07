@echo off
REM Daily swing autopilot: screen -> journal paper trades -> resolve older
REM ones -> update the learner. Zero API keys (yfinance EOD). Scheduled
REM weekdays 16:30 IST (after close). Nothing here places orders.
cd /d C:\Users\yashs\trading_system
set PYTHONIOENCODING=utf-8
python swing_screen.py --journal --json >> logs\swing_task.log 2>&1
python swing_tracker.py >> logs\swing_task.log 2>&1
