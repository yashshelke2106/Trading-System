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

## Dhan API Status — EXPIRED (2026-07-24)
**The Dhan Data API subscription has expired.** Every `/v2/charts/*` call
returns HTTP 401. Consequences, verified:
- `core/api_dhan.py` daily/intraday → dead
- `core/market_feed.py` (WebSocket LTP/volume/**order-flow queues**) → dead.
  Order flow, bid/ask and L2 depth are therefore NOT capturable at all.
- `core/market_bias.py` fell back to **randomly generated bars** and scored
  them a confident LONG_BIAS. Fixed: synthetic frames are now tagged
  `df.attrs["synthetic"]` and force `MarketBias.NEUTRAL` + `ctx.synthetic=True`.
- `core/sector_rotation.py` routed to the dead Dhan endpoint, so
  `check_sector_alignment()` failed open and passed 100% of trades. Fixed:
  yfinance fallback restored (matches its own docstring).

Working data sources (probed live 2026-07-24, market open):
| Source | Status |
|---|---|
| yfinance daily | OK |
| yfinance intraday 5m | OK (~3 min behind live; **60-day max lookback**) |
| NSE bhavcopy archive | OK (real zip, full CM segment) |
| Google News RSS | reachable but STALE (freshest ~68h, most 800-1300h) — unusable |

Ticker map edge cases: TATAMOTORS→TMCV.NS, MCDOWELL-N→UNITDSPR.NS, DEEPAKNT→DEEPAKNTR.NS

## Market Capture Layer
Point-in-time recorder. **Not** a signal generator — the alpha hunt is a
closed negative; this exists so future work is data-rich and survivorship-honest.

| Layer | Module | Archive | Recoverable later? |
|---|---|---|---|
| EOD full universe | `bhavcopy_archive.py` | `logs/bhavcopy_archive/` | Yes — back-fillable for years |
| Intraday 5m | `core/intraday_capture.py` | `logs/intraday_5m/` | **NO — 60-day window, then gone forever** |
| Day movement shape | `core/day_structure.py` | `logs/day_structure/` | derived from 5m (so inherits the 60-day rule transitively) |
| Swing events | `core/swing_structure.py` | `logs/swing_events.jsonl` | events point-in-time; re-derivable from EOD archive |
| Trend state | `core/market_state.py` | `logs/capture_state.json` | derived |

Movement layers are DESCRIPTIVE only (day_type / pivots / breakout events =
what the tape did). Swing pivots are timestamped at CONFIRMATION (pivot+5
bars) — consuming them keyed on pivot_date is lookahead. Breakout events carry
forward-return slots filled later by `--analyze`; they are evidence for a
future statistician gate, not entries. Partial sessions recompute for 5 days
(`RECOMPUTE_DAYS`) so a mid-day capture can't freeze a wrong label.

Daily job: `python capture_task.py` (run after 16:00 IST).
Endpoints: `GET /api/capture`, `GET /api/market-state`.

EOD archive holds 2019-01-01 → present, **3,526 symbols including delisted**
(survivorship-complete — yfinance shows survivors only and inflates PF 1.5-2×).

Merge rule is point-in-time: **the bar already on disk wins**. After a split,
newly fetched bars arrive adjusted while captured bars stay as-quoted, so a
series can straddle an adjustment boundary. `conflicts` in the capture summary
is the tripwire — non-zero means a corporate action.

`market_state` KNOWN LIMITATION: these indicators cannot separate a trend from
a lucky random walk. ADX(14) lands ~18-30 on pure noise. Labels describe what
the tape has done, never what it will do.

## NSE Market Hours
09:15 – 15:30 IST, Mon–Fri. Expiry: weekly Thursday (stocks), monthly last Thursday (index).

## Never Touch Without Thinking
- `config.py` `PAPER_TRADE` flag — must stay True unless explicitly going live
- `core/risk_engine.py` daily loss formula — was buggy before, verify ratio not raw rupee
- `core/execution.py` direction map — long→BUY, short→SELL for Dhan API
