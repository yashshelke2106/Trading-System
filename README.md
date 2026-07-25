# F&O Signal Terminal

A research-first NSE Futures & Options signal system for the Indian market —
scanner, backtest harness, risk engine, options tooling, and a live signal
dashboard. Built as a personal quant desk: it generates and *validates* trading
ideas before it ever risks money, and it stays honest about what actually works.

> **Status: signal-only, paper-trade.** `PAPER_TRADE=True` in `config.py` — the
> system places **no real orders**. It scans, ranks, and displays signals; any
> execution is manual, in a separate broker app. See [Honest status](#honest-status).

---

## What it does

- **Scans** the NSE F&O universe intraday and writes graded signals to `logs/signals.json`.
- **Votes** patterns into confluence signals (needs 3+ votes with a 2-vote lead).
- **Ranks** candidates and gates them through volatility, fake-breakout, order-flow,
  strike-selection, an AI filter, and a risk engine before anything surfaces.
- **Serves** signals to a Next.js dashboard (`:3000`) via a FastAPI backend (`:8000`),
  plus a legacy Streamlit view.
- **Backtests** every strategy on real data with cost-aware, survivorship-honest metrics.
- **Captures** point-in-time market data (EOD + intraday) so future research is data-rich.
- **Allocates** — a capital-allocation engine (`core/allocation.py`) that captures the
  equity premium cheaply (index-core + optional 200-DMA overlay). This is the current
  money path, not an intraday alpha claim.

---

## Stack

Python 3.x · Next.js 16 (`trading-ui/`) · FastAPI (`api_server.py`) · scikit-learn ·
pandas / numpy · Dhan API (with yfinance fallback) · NSE bhavcopy archive.

---

## Quick start

**Windows, from the project root (`C:\Users\yashs\trading_system`).**
Full command reference lives in [`README_RUN.md`](README_RUN.md).

```bat
REM one-time setup
python setup_dhan_keys.py          REM enter Dhan keys (Trading + Data API)
cd trading-ui && npm install && cd ..

REM verify data is flowing
python verify_dhan.py

REM run the live signal system + UI + browser (PAPER_TRADE stays ON)
start_pullback.bat

REM stop everything
stop_trading.bat
```

Dashboards once running:

| UI | URL |
|----|-----|
| Next.js (main)     | http://localhost:3000 |
| API backend        | http://localhost:8000 |
| Streamlit (legacy) | http://localhost:8501 |

---

## Data flow

```
Scanner → Market Bias → Time Filter → Signal Engine → Volatility Filter
→ Fake-Breakout Filter → Order Flow → Strike Selection → AI Filter
→ Trade Ranker → Risk Engine → (paper) Execution
```

## Key files

| File | Purpose |
|------|---------|
| `config.py`                    | All thresholds, API config, `PAPER_TRADE=True` |
| `scan_only_v2.py`              | Scanner — writes `logs/signals.json` |
| `api_server.py`                | FastAPI on `:8000` (REST + WebSocket) |
| `trading-ui/`                  | Next.js dashboard on `:3000` |
| `core/signal_engine.py`        | Pattern voting (3+ votes, 2-vote lead) |
| `core/trade_ranker.py`         | Signal 25% · order-flow 20% · breakout 18% · liquidity 17% |
| `core/risk_engine.py`          | Daily loss = P&L / capital (ratio, not raw rupee) |
| `core/fake_breakout_filter.py` | VWAP + volume dry-up + spring detection |
| `core/api_dhan.py`             | Dhan API + yfinance fallback |
| `core/allocation.py`           | Capital-allocation engine (index-core + 200-DMA overlay) |
| `signal_tracker.py`            | Tracks signal outcomes → `logs/signal_journal.jsonl` |

## Repository layout

```
core/            strategy, risk, data, filters, allocation engines
trading-ui/      Next.js dashboard
brain/           research notes / decision log (the "project brain")
docs/            portfolio, research writeups
logs/            signals, journals, metrics, market-capture archives
models/          persisted ML artifacts
tests/           test suite
*.py             backtests, research scripts, capture tasks, runners
*.bat            Windows launchers (start / stop / verify)
```

---

## Data sources

The **Dhan Data API subscription is expired** — every `/v2/charts/*` call returns
HTTP 401, so live order-flow / L2 depth are not currently capturable. The system
falls back cleanly:

| Source | Status |
|--------|--------|
| yfinance daily         | OK |
| yfinance intraday 5m   | OK (~3 min behind live; **60-day max lookback**) |
| NSE bhavcopy archive   | OK — real CM segment, 2019→present, 3,526 symbols incl. delisted |
| Dhan charts / WebSocket| **Dead** (subscription expired) |

The bhavcopy archive is **survivorship-complete** — yfinance shows survivors only
and inflates profit factor 1.5–2×, which this system measures and corrects for.

---

## Honest status

This project's research history is deliberately recorded, including the negatives:

- **No validated intraday alpha edge** was found in the liquid F&O large-cap universe —
  gap-fade, PEAD, vol-managed, index-rebalance, and RSI-2 mean-reversion were all
  tested and rejected at realistic costs. The universe is too efficient. This is an
  honest exhaustive negative, not a pending win.
- The **active, defensible money path** is capturing the equity premium cheaply via
  `core/allocation.py` (index-core + optional 200-DMA overlay) — earn ~market, not alpha.
- `PAPER_TRADE=True` is a hard default and must stay that way unless explicitly going live.
- Metrics are cost-aware and survivorship-honest by design; "profit factor" headline
  numbers are cross-checked against premium-% mirages and open-fill artifacts.

## Never change without thinking

- `config.py` → `PAPER_TRADE` — must stay `True` unless deliberately going live.
- `core/risk_engine.py` daily-loss formula — it's a **ratio** (P&L / capital), not raw rupee.
- `core/execution.py` direction map — long → BUY, short → SELL for the Dhan API.

---

## Disclaimer

For personal research and education only. This is **not** investment advice and makes
**no** guarantee of returns. Trading F&O carries substantial risk of loss. Nothing here
should be construed as a recommendation to buy or sell any security. Use at your own risk.
