# F&O Signal Terminal — Project Brain

## Stack
Python 3.x, Next.js 16 (trading-ui/), FastAPI (api_server.py), Dhan API + yfinance fallback, scikit-learn, pandas/numpy

## Quick Start
```
start_trading.bat     ← opens 3 windows + browser (http://localhost:3000)
stop_trading.bat      ← kills all 3 processes
```

## Key Entry Points
- `scan_only_v2.py` — scanner, writes `logs/signals.json` (enriched with option leg)
- `api_server.py` — FastAPI on :8000 (REST + WebSocket)
- `trading-ui/` — Next.js UI on :3000
- `streamlit run streamlit_app.py` — legacy dashboard (still works)
- `live_runner.py` — live execution loop (paper trade mode)
- `signal_tracker.py` — tracks signal outcomes to `logs/signal_journal.jsonl`

## Critical Files
| File | Purpose |
|------|---------|
| `config.py` | All thresholds, API config, `PAPER_TRADE=True` |
| `core/signal_engine.py` | Pattern voting (needs 3+ votes, 2-vote lead) |
| `core/trade_ranker.py` | Weights: signal 25%, order_flow 20%, breakout 18%, liq 17% |
| `core/risk_engine.py` | Daily loss = P&L / capital (not raw rupee vs 0.05) |
| `core/fake_breakout_filter.py` | VWAP + volume dry-up + spring detection |
| `core/api_dhan.py` | Dhan API + yfinance fallback (_yfinance_intraday, _yfinance_daily) |
| `core/dashboard_data.py` | All data fetching for Streamlit |
| `core/secrets.py` | Keyring-first credential store |

## Data Flow
Scanner → Market Bias → Time Filter → Signal Engine → Volatility Filter
→ Fake Breakout Filter → Order Flow → Strike Selection → AI Filter
→ Trade Ranker → Risk Engine → Execution

## Credentials
- Trading token: `dhan_token.txt` (JWT, refresh daily)
- Data API key: `.dhan_data_apikey` or keyring
- Client ID: `.dhan_client_id` or keyring

## Signal File Schema
`logs/signals.json` → `{ts, count, by_grade: {A,B,C}, meta, signals: [...]}`
Each signal: `{symbol, direction, entry_price, sl_price, target_price, rr_ratio, confluence_grade, confluence_score, patterns_combined, reason, ts}`

## Current Mode
**Signal-only**: scan → signals.json → Streamlit dashboard → manual execution in Dhan app.
`PAPER_TRADE=True` in config.py — no real orders even if live_runner.py is running.

## Dhan API Status
Data API: SUBSCRIBED + auth OK (NOT DH-902 — that note was stale/wrong).
Live diagnosis: requests reach Dhan, auth passes, per-field validation
works (omit a field → specific "X is required"). Blocker is **DH-905**
(Dhan rejects request param VALUES) on the documented v2 schema — a
request-mechanics mismatch vs the official `dhanhq` SDK, not a dead API.
Fixed: `_segment_for` now returns `IDX_I` (was wrong `NSE_IDX`).
Remaining DH-905 must be closed on a real machine using the dhanhq SDK
as reference (this sandbox has SSL interception + a simulated 2026
clock that block a clean end-to-end Dhan call). yfinance fallback
active meanwhile — adequate for SWING (daily bars).
Ticker map edge cases: TATAMOTORS→TMCV.NS, MCDOWELL-N→UNITDSPR.NS, DEEPAKNT→DEEPAKNTR.NS

## NSE Market Hours
09:15 – 15:30 IST, Mon–Fri. Expiry: weekly Thursday (stocks), monthly last Thursday (index).

## Never Touch Without Thinking
- `config.py` `PAPER_TRADE` flag — must stay True unless explicitly going live
- `core/risk_engine.py` daily loss formula — was buggy before, verify ratio not raw rupee
- `core/execution.py` direction map — long→BUY, short→SELL for Dhan API
