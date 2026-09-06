@echo off
REM Daily macro fund cycle (H-022): mark the book, rebalance if the month
REM turned, append the NAV point. Zero API keys (yfinance EOD).
REM
REM Schedule WEEKDAYS AT 03:00 IST, not 16:30 like the swing task. The book
REM holds US-listed ETFs, so a mark taken at the Indian close lands mid-session
REM in New York — marking a forming bar, which is the exact corruption the
REM capture layer already fights on the Indian side. 03:00 IST is after the
REM 16:00 ET close on the previous US trading day.
REM
REM Nothing here places an order.
cd /d C:\Users\yashs\trading_system
set PYTHONIOENCODING=utf-8
python macro_task.py >> logs\macro_task.log 2>&1
python macro_screen.py --journal --json >> logs\macro_task.log 2>&1
