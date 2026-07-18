@echo off
REM Daily event/news system update (snapshot + collect + analyze + weekly refresh)
cd /d "%~dp0"
python -m scripts.events_update %*
