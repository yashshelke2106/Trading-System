---
tags: [measurement]
---
# Gap-Honest Fills

If a bar OPENS beyond your stop, you fill at the OPEN — worse than the stop price ([[Gap Risk]]). The old backtest filled at the stop and hid **+1.2R** of losses; intrabar ambiguity resolved stop-first (conservative).

Impact: [[India Swing Strategy]] PF 0.72 → **0.50** under honest fills. Every later harness ([[Breakout Strategies]], bakeoffs) used this engine, verified by an offline selftest.

Principle: simulate the fill the market would actually give, not the one your rules wish for.
