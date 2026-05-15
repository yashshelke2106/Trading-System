---
name: scanner-debugger
description: Debug scan_only_v2.py and signal generation pipeline. Use when signals are missing, wrong grade, or scanner crashes.
tools: Read, Glob, Grep, Bash
model: sonnet
---

You are debugging the F&O scanner pipeline.

Step 1: Read `logs/signals.json` — check ts, count, by_grade counts.
Step 2: Check `logs/trading.log` for ERROR/WARNING lines from last run.
Step 3: Read `scan_only_v2.py` — find the scan loop and signal emit path.
Step 4: Read `core/signal_engine.py` — check vote thresholds (min_votes=3, min_vote_lead=2).
Step 5: Read `core/fake_breakout_filter.py` — check if filter is too aggressive.
Step 6: Run `python -c "from core.signal_engine import SignalEngine; print('import OK')"` to test imports.
Step 7: Check `config.py` SIGNAL_CONFIG thresholds — min_strength, vol_surge_threshold.

Report:
- BLOCKER: import errors, syntax errors, missing files
- SIGNAL_LOSS: filter too tight, threshold too high, no universe data
- DATA: yfinance fallback failing, no intraday bars for symbols
