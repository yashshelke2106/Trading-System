@echo off
REM Legacy Streamlit dashboard (needs Dhan token for live F&O data).
REM Pinned to 8503 so it can NEVER collide with the Swing Finder (8501).
REM GUARD: refuses to start a duplicate (Windows allows silent port sharing).
cd /d C:\Users\yashs\trading_system
set PYTHONIOENCODING=utf-8

netstat -aon | findstr /C:":8503" | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
    echo Legacy dashboard already running -- opening browser instead of a duplicate.
    start http://localhost:8503
    exit /b 0
)

echo Starting LEGACY dashboard on http://localhost:8503 ...
streamlit run streamlit_app.py --server.port 8503
