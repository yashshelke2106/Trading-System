@echo off
REM No-API swing finder — needs zero credentials (free yfinance data).
REM Pinned to 8501. GUARD: Windows lets two Streamlit processes silently
REM SHARE a port (double-bind) — the cause of "wrong dashboard" bugs — so
REM if 8501 is already serving, we open the browser instead of a duplicate.
cd /d C:\Users\yashs\trading_system
set PYTHONIOENCODING=utf-8

netstat -aon | findstr /C:":8501" | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
    echo Swing Finder already running -- opening browser instead of a duplicate.
    start http://localhost:8501
    exit /b 0
)

echo Starting Swing Finder on http://localhost:8501 (no API keys required)...
streamlit run swing_app.py --server.port 8501
