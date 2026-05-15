@echo off
echo Stopping F^&O Terminal...

taskkill /FI "WINDOWTITLE eq FO-Aladdin*"    /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq FO-UI*"         /T /F >nul 2>&1
REM Legacy window names (kept for back-compat)
taskkill /FI "WINDOWTITLE eq FO-Scanner*"    /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq FO-Executor*"   /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq FO-API*"        /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq FO-Tracker*"    /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq FO-Streamlit*"  /T /F >nul 2>&1

REM Kill by port as fallback (API 8000, UI 3000, Streamlit 8501)
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8000" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":3000" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8501" ^| find "LISTENING"') do taskkill /PID %%a /F >nul 2>&1

REM Final safety net — kill any orphan python/streamlit
taskkill /F /IM streamlit.exe /T >nul 2>&1

echo Done.
