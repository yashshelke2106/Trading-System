# How to run — command reference

Everything you need to start, stop, view, and backtest the F&O system.
Windows. Run from the project root: `C:\Users\yashs\trading_system`.

---

## 🟢 Easiest — double-click (no typing)

| Double-click | What it does |
|--------------|--------------|
| `verify.bat`         | Checks Dhan data → PASS/FAIL report → offers to run the backtest |
| `start_pullback.bat` | **The real system** — futures mode, longs-only, regime-gated + UI + browser |
| `start_trading.bat`  | Full legacy system (scanner + executor + API + UI) |
| `stop_trading.bat`   | Kills all running processes/ports |

---

## 0. First-time setup (once)

```
python setup_dhan_keys.py          REM enter Dhan keys (Trading + Data API)
cd trading-ui && npm install       REM install UI dependencies
cd ..
```

---

## 1. Verify Dhan data (do this first, any time)

```
python verify_dhan.py
```
PASS = data flowing, ready to trade/backtest. FAIL = fix keys/subscription
(the script prints the specific hint).

---

## 2. Run the live system (signals + UI)

**Recommended — stripped pullback system (futures, longs-only):**
```
start_pullback.bat
```
Launches: scanner (futures mode) + signal tracker + API (:8000) + UI (:3000)
+ opens the browser. `PAPER_TRADE` stays ON — no real orders.

**Manual (4 separate terminals) if you prefer:**
```
python scan_only_v2.py
python signal_tracker.py
python -m uvicorn api_server:app --reload --port 8000
cd trading-ui && npm run dev
```

**Full legacy system:**
```
start_trading.bat
```

**Stop everything:**
```
stop_trading.bat
```

---

## 3. The UI / dashboards

| UI | URL | Command |
|----|-----|---------|
| Next.js (main)     | http://localhost:3000 | `cd trading-ui && npm run dev` |
| API backend        | http://localhost:8000 | `python -m uvicorn api_server:app --reload --port 8000` |
| Streamlit (legacy) | http://localhost:8501 | `streamlit run swing_app.py --server.port 8511` |

---

## 4. Backtests

**india_swing — the pullback edge, on real Dhan data (Windows CMD):**
```
set ONLY_LONG=1& set PRECISION_MODE=0& set DISABLE_G9=1& set DISABLE_G10=1& set DISABLE_REGIME_GATE=1& python backtest_india_swing.py
```

PowerShell version:
```
$env:ONLY_LONG=1; $env:PRECISION_MODE=0; $env:DISABLE_G9=1; $env:DISABLE_G10=1; $env:DISABLE_REGIME_GATE=1; python backtest_india_swing.py
```

**ORB backtest:**
```
python backtest_orb.py
```

Read the results block at the end: **Profit factor** (want > 1.0),
**Expectancy** (want positive), **Total trades** (want > 30).

---

## 5. Analysis & maintenance

| Task | Command |
|------|---------|
| Universe filter — who's blocked | `python -m core.universe_filter` |
| Write today's metrics line       | `python -m core.metrics_writer` |
| Walk-forward param fit (monthly) | `python walk_forward_fit.py` |
| Quick signal peek (any time)     | `python scan_only_v2.py --force --top 10` |

`--force` runs the scanner outside market hours (testing only — drop it
during live 09:15–15:30).

---

## 6. Daily routine (the 4-week forward test)

```
1. start_pullback.bat        before 09:15
2. let it run all day        signals -> UI, outcomes -> logs/signal_journal.jsonl
3. stop_trading.bat          after 15:30 close
4. metrics auto-write 15:31  check logs/metrics_daily.jsonl for drift_alert
```

Do NOT tune parameters mid-test. After >= 30 forward trades:
- 30d PF < 1.0  -> kill the setup (don't tune)
- 30d PF > 1.2  -> promote, size up at fixed 1% risk/trade

---

## Key config (config.py — local, gitignored)

| Flag | Default | Meaning |
|------|---------|---------|
| `PAPER_TRADE`            | `True`    | simulated fills, no real orders |
| `INSTRUMENT_MODE`        | `futures` | `futures` or `options` |
| `FUT_COST_ROUNDTRIP_PCT` | `0.06`    | net-of-cost % subtracted per trade |

Env overrides (set before a command): `ONLY_LONG`, `PRECISION_MODE`,
`INSTRUMENT_MODE`, `DISABLE_G9`, `DISABLE_G10`, `DISABLE_REGIME_GATE`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `python` not found        | use `py` instead of `python` |
| Backtest empty / no bars  | run `python verify_dhan.py` — Dhan not feeding |
| NIFTY/regime blank        | fix NIFTY security-id + `IDX_I` segment in scrip master |
| UI won't load             | `cd trading-ui && npm install` then `npm run dev` |
| Port already in use       | `stop_trading.bat` first |
