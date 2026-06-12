# Trading System — Strategy Documentation (v4, 2026-06-10)

> Rewritten to match the **actual running code**. The previous v3 doc described
> a dead options pipeline ("65-70% WR 1:3", AI-filter thresholds) that no longer
> exists. For the full audit, edge findings, and ratings see
> **`AUDIT_AND_EDGE_HUNT.md`**.

## Current mode (what actually runs)

- **Instrument:** stock **FUTURES** (`config.INSTRUMENT_MODE="futures"`). The
  options era was abandoned — premium decay/IV were killing stops while the
  spot direction was often right.
- **Strategy:** `core/strategy_india_swing.py` — a daily-close swing strategy,
  evaluated **once per day** (`config.ISWING_DECISION_TIME`), not intraday.
- **Execution:** `PAPER_TRADE=True`. Signal-only — the scanner writes
  `logs/signals.json`; a human reviews/executes manually. **No real orders.**
- **Honest status:** no validated, cost-surviving, out-of-sample edge has been
  found (directional or volatility). The system is a *paper-research platform*,
  not a deployable money-maker. Do **not** loosen the gates to manufacture
  trades — trade scarcity is the symptom of no edge, not a bug.

## Pipeline (live)

```
scan_only_v2.py (once/day for india_swing)
  → strategy_india_swing: G0 regime → G1 trend → G2 pullback → G3 confirm+vol
       → G4 structural stop → G5 RSI/RS → G6 sector → G7 earnings
       → G8 delivery → G9 sector-leader → G10 ML  (all must pass)
  → futures_leg (attach lot/notional)  → entry_guard
  → finalize_and_select (calibrate + sector cap + count cap)
  → pullback_entry queue → monte_carlo enrich → R:R position sizing
  → signal_writer → logs/signals.json
```
Note: the ~11 gates in series make this **very** low-frequency by design
(PRECISION_MODE ≈ 2 trades / 30 names / 2yr; full stack ≈ 0). That is expected.

## Risk engine (`core/risk_engine.py`)

- Risk/trade 1.2% of capital; daily-loss kill-switch **4%** (now includes open
  MTM); max 2 consecutive losses; max 3 trades/day; per-symbol same-day
  re-entry block; **sector correlation cap** (`max_per_sector=2`).
- Stops: ATR×1.2 bounded to [1.0%, 2.5%] of entry; target by R:R (min 1.5).
- Lot sizes: **live from the Dhan scrip master** (`core/scrip_master.lot_size`),
  falling back to `config.NSE_LOT_SIZES`.

## Measurement & monitoring (the honest layer — the system's strongest part)

- `core/metrics_writer.py` — daily metrics on the **trustworthy** directional
  pnl (option-premium rows excluded); drift alarm fires on PF<0.9, PF>3
  (implausible), DD>8%, or <20 clean trades.
- `honest_metrics.py` — re-score the live journal honestly.
- In-session self-tuning is **frozen** (`config.LEARNING_ENABLED=False`).

## Research / rigor harnesses (run on your machine; Dhan-only data)

| File | What it tests |
|---|---|
| `backtest_live_pipeline.py` | gap-honest, point-in-time, live-parity backtest (`--selftest` offline) |
| `edge_research.py` / `edge_hunt.py` | momentum / trend / mean-rev / overnight / low-vol / seasonality / VIX battery |
| `validate_meanrev.py` (+`_liquid`) | RSI-2 mean-reversion with a cost sweep |
| `pairs_program.py` | diversified market-neutral cointegration stat-arb (IS-select / OOS-trade) |
| `research_vrp.py` / `research_iron_condor.py` | volatility risk premium (HAC errors, tail-aware) |

All use fixed a-priori params, held-out splits, cost sweeps, and verdicts that
can say "no edge". **Discipline:** never tune params to make a backtest pass.

## Tests

`tests/` — pytest covering risk engine, the honest monitor, the gap-honest exit
engine, indicators, and config invariants. Run: `python -m pytest tests/ -q`.

## Key files

| File | Purpose |
|---|---|
| `config.py` | thresholds, `PAPER_TRADE`, safety flags (`LEARNING_ENABLED`, `GATES_FAIL_CLOSED`, `REQUIRE_EARNINGS_DATA`, `ISWING_DECISION_TIME`) |
| `scan_only_v2.py` | live scanner → `logs/signals.json` |
| `core/strategy_india_swing.py` | the 11-gate swing strategy |
| `core/risk_engine.py` | sizing, stops, daily kill-switch, correlation cap |
| `core/metrics_writer.py` | honest daily metrics + drift alarms |
| `core/api_dhan.py` | Dhan data (daily/intraday, dedup'd) |
| `core/scrip_master.py` | live security IDs + lot sizes |
