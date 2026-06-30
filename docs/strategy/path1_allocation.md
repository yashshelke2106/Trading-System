# Path-#1 Strategy: Capture the Equity Premium Cheaply

**Status: ACTIVE strategy (paper).** Replaces alpha-hunting after an exhaustive,
rigorous edge hunt found no robust tradeable edge in this universe.

## Why this, and why now

The desk tested — and rigorously rejected — every accessible edge:

| Universe | Tested | Verdict |
|---|---|---|
| F&O large-cap | entry/factor/regime, intraday gap-fade, PEAD | efficient — no edge |
| Midcap | PEAD | real but trapped in illiquidity (limits-to-arbitrage) |
| Cash-equity / F&O | RSI-2 mean-reversion | real but untradeable (overnight-gap fill + illiquid-only) |

Every apparent edge died to **cost, power, or limits-to-arbitrage**. That is a
coherent, evidence-backed conclusion: there is no manufacturable alpha here. The
rational response is to capture the **equity risk premium** — which is real, large
over time, and free of any edge requirement — at minimal cost, with disciplined
drawdown control. (See docs/research/*.md for the full reject record.)

## The allocation

**Core (the dependable thing): a low-cost broad-index ETF.**
- India: **NIFTYBEES** (Nifty 50, ~0.03–0.05% expense) or a **Nifty 500 ETF** for breadth.
- This *is* the equity premium. No signal, no timing skill required.
- Default target: **100% equity** when risk-on.

**Optional drawdown overlay (200-DMA trend) — a risk-tolerance choice, measured not assumed.**
- Rule: monthly, if NIFTY < its 200-day MA, **de-risk equity to 50%** (rest in liquid/cash); restore to 100% when back above.
- Measured profile (2018–2026, net of cost): same Sharpe, **drawdown −23% vs −38.5%** for ~1% CAGR — a good trade if you can't ride a 38% drop.

**Optional factor sleeve — ONLY via real factor-INDEX ETFs, never the backtest.**
- The in-sample factor outperformance (momentum 22% CAGR / 0.77 Sharpe) is
  **survivorship-inflated** (today's constituents) and cross-sectional momentum was
  already rejected under proper testing. Do **not** trade a self-built factor model.
- If you want factor exposure: a small (≤20%) sleeve in **Nifty200 Momentum 30** /
  **Nifty Low Vol 30** index ETFs — live, non-survivorship track records, humble
  expectations.

## Measured profiles (your data, 2018–2026; NIFTY = proxy for the ETF)

| Variant | CAGR | Sharpe | maxDD |
|---|---|---|---|
| Index core (buy & hold) | 10.7% | 0.24 | −38.5% |
| Core + 200-DMA overlay (de-risk to 50%) | 9.7% | 0.24 | **−23.2%** |

*(This 8-year window includes the COVID crash; the long-run Indian equity premium
is typically higher, ~12–14%.)*

## How it runs in this system

- **`allocation_monitor.py`** — prints today's target allocation + the backtest.
  `python allocation_monitor.py` (core) or `--overlay` (with drawdown control).
- **`core/allocation.py`** — the engine: `compute_target_allocation()` (today's
  target), `backtest_allocation()` (honest, net of cost), config in `ALLOCATION_CONFIG`.
- **Risk engine** (`core/risk_engine.py`, hardened): use its drawdown halt and
  position sizing on the whole allocation; `PAPER_TRADE=True` until you go live.
- **Honest measurement**: track realized vs target via the journal /
  `core/honest_performance.py` — net P&L, no premium-% mirage.

## Honest expectations

You earn **approximately the market return**, with low cost and discipline — **not
alpha**. That is the truthful, evidence-based outcome, and it decisively beats the
net-negative active strategies (PF 0.49–0.72) the system ran before. Discipline,
low cost, diversification, and drawdown control are the edge here — not prediction.

## Rebalancing & operations
- Rebalance **quarterly** (or on a trend-state change) — keep turnover low; cost is
  the one thing you fully control.
- Don't override the rule on a hunch; the whole point is removing discretion.
- Re-confirm factor-sleeve decisions on the survivorship-complete bhavcopy archive
  (full 2018–present, on a real machine) before allocating real money to a tilt.
