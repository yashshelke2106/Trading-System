@echo off
REM Streamlit event/news dashboard (read-only) - http://localhost:8503
cd /d "%~dp0"
python -m streamlit run events_app.py --server.port 8503 --server.headless true
