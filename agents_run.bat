@echo off
REM Agentic AI runners (all risk-reducing only). Usage:
REM   agents_run.bat sentinel   - halt-only risk sentinel (daily pre-open)
REM   agents_run.bat macro      - macro-regime advisory (weekly)
REM   agents_run.bat intake     - research-intake proposals (weekly)
REM   agents_run.bat clear      - human override: clear sentinel halt
cd /d "%~dp0"
if "%1"=="sentinel" python -m scripts.agent_risk_sentinel %2 %3
if "%1"=="macro"    python -m scripts.agent_macro_regime %2 %3
if "%1"=="intake"   python -m scripts.agent_research_intake %2 %3
if "%1"=="clear"    python -m scripts.agent_risk_sentinel --clear
if "%1"=="" echo usage: agents_run.bat sentinel^|macro^|intake^|clear [--dry-run]
