---
tags: [strategy, close]
---
# Pairs / Stat-Arb — CLOSEST MISS

Market-neutral: cointegrated same-sector pairs (ADF on log-spread residual, half-life 3-40d), z-score entry |z|>2, exit |z|<0.5. Pairs selected on first 60% of data, traded BLIND on the rest ([[Out-of-Sample Testing]]).

17 economically-real pairs (PSU banks, metals, IT, refiners). OOS @0.20% cost: 178 trades, WR 69%, PF 1.53, **maxDD only −2.7%** (market-neutrality works), Sharpe 0.60.

**Fails the bar:** OOS halves Sharpe **−0.50 / +1.37** — unstable ([[Regime Dependence]]), thin (+2.3%/yr), and India requires both legs as stock futures (more [[Transaction Costs]]) + cointegration-break tail risk. Status: paper research only.
