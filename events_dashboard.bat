@echo off
REM Event/news dashboard: risk flags, event calendar, archive stats (read-only)
cd /d "%~dp0"
python -m scripts.events_dashboard %*
