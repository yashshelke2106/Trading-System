@echo off
REM FULL system update: prices + events + news + reactions + study (one command)
cd /d "%~dp0"
python -m scripts.system_update %*
