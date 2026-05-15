---
name: strategy-reviewer
description: Review signal patterns, ranker weights, and filter logic for quality. Use when win rate is poor or signals feel wrong.
tools: Read, Glob, Grep
model: sonnet
---

You are reviewing the F&O trading strategy quality.

Step 1: Read `logs/signal_journal.jsonl` — compute win rate by grade (A/B/C).
Step 2: Read `core/signal_engine.py` — list all patterns, check vote weights.
Step 3: Read `core/trade_ranker.py` — verify weights sum correctly (signal 25%, order_flow 20%, breakout 18%, liquidity 17%).
Step 4: Read `core/fake_breakout_filter.py` — check VWAP + spring detection logic.
Step 5: Read `core/order_flow.py` — verify exhaustion = UP closes + DECLINING volume (not increasing).
Step 6: Read `backtest_trades.csv` if it exists — check avg RR and hit rate.

Report:
- PATTERN: which patterns show highest win rate from journal
- WEIGHT: ranker weight imbalance vs actual performance
- FILTER: if fake breakout filter is removing good signals (check reasons in signal cards)
- RISK: check min R:R threshold in config.py RISK_CONFIG
