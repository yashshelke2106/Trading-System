@echo off
REM ============================================================
REM  DEPRECATED (2026-07-07). This launcher used to:
REM    - kill ALL python processes (breaking scheduled tasks)
REM    - RESET learner weights on every launch (destroying the
REM      SwingLearner's accumulated knowledge)
REM    - hard-require dhan_token.txt
REM    - start the LEGACY Streamlit dashboard on port 8501,
REM      colliding with the Swing Finder
REM  Use run_system.bat (everything) or start_ui.bat (UIs only).
REM  Forwarding to run_system.bat...
REM ============================================================
echo run_all.bat is DEPRECATED - forwarding to run_system.bat
timeout /t 3 /nobreak >nul
call run_system.bat
