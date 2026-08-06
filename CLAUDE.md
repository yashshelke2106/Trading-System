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

## Dhan API Status — LIVE again (2026-08-04)
**The Dhan Data API subscription is ACTIVE again** (was expired 2026-07-24 →
2026-08-03). `test_data_api()` → "Active"; `get_intraday_data` returns real 5m
bars; `get_quote` returns live LTP + L2 **depth**. Three live-quote bugs were
found and fixed on restore (commit for this):
- **Endpoint casing** — `/marketFeed/quote` → `/marketfeed/quote` (Dhan router is
  case-sensitive; capital-F 404s). Same fix in `get_option_quote`.
- **Security-id type** — `get_quote` must send integer security-ids, not strings.
- **Response parser** — Dhan nests `data → SEGMENT → securityId → fields`; the old
  parser stopped at segment level so `last_price` was always 0.

Honest paper fills are now wired (`core/execution.py` `_paper_fill`): a PAPER fill
is priced off the live quote and CROSSES the spread + slippage
(`config.PAPER_FILL_COST_BPS`), never `price or 2500`. No live order is ever sent
(PAPER_TRADE gate intact).

Still true from the outage era: `core/market_bias.py` synthetic-frame guard
(`df.attrs["synthetic"]` → force NEUTRAL) and `core/sector_rotation.py` yfinance
fallback remain in place as defenses. `core/market_feed.py` WebSocket order-flow
queues were not re-verified live — re-probe before relying on order flow / L2.

Verified 2026-08-06: Dhan historical serves a **full 10 years** of daily bars
(2,623 from 2016-01-01; `dhan_daily(days_back=3650)` → 2,476 rows). It is NOT
depth-capped. HTTP 400 storms are a **symbol-rename** problem, not auth:
`get_security_id()` passes the SYMBOL itself when the scrip-master lookup
misses, and Dhan rejects that. Aliases live in `core/scrip_master.py`
`_RENAMES` (TATAMOTORS→TMCV, MCDOWELL-N→UNITDSPR, IDFC→IDFCFIRSTB,
HPCL→HINDPETRO). LTIM and GUJGASLTD are genuinely delisted —
`DELISTED_NO_SUCCESSOR`; prune them from FO_UNIVERSE.

Deep history for research comes from the **local archive**, not the backtest:
`python -m scripts.build_history_archive --refresh --period 10yr` →
`data/history/` (357k bars / 153 symbols, real OHLC). `backtest_india_swing.py`
is Dhan-only by design and does NOT read that archive.

**The ML filter is deliberately OFF.** `logs/ml_filter_model.REJECTED_2026-08-06.pkl`
was measured on holdout as *worse than useless*: gating at P≥0.55 gave
−3.04%/trade vs −1.20% ungated (accuracy 0.474 vs a 0.868 always-loss
baseline). `check_ml_filter()` fails OPEN, so no model file = pass-through =
the safe state. Do not retrain on `backtest_india_swing_trades.csv` — it holds
~126 usable rows and the base signal is net-negative.

Data sources (yfinance fallbacks still valid when Dhan hiccups):
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
