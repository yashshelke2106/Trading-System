# Audit, Honest Fixes & Edge Hunt — Process Record (2026-06-09)

A self-contained record of a full adversarial audit, the honest fixes applied,
and an exhaustive edge hunt. Read this first in any future Claude Code session
before touching strategy/edge code, so the work isn't re-derived (or, worse, so
the `PF 16` mirage isn't trusted again).

---

## TL;DR

- **No economically-tradeable, out-of-sample-stable, cost-surviving edge** was
  found in this system's accessible space (Indian F&O, daily bars, retail cost).
- Real *signals* exist (mean-reversion, overnight drift, VRP, pairs) — but each
  dies to **transaction cost**, **tail/regime risk**, or **instability**.
- System rating: **2/10 → 4.0/10** after honest fixes (plumbing). Edge: **1 → 2**
  ("real signals exist, none exploitable"). Keep `PAPER_TRADE=True`.
- The single most useful action for capital: **buy the proven edge cheaply**
  (index + factor ETFs), don't build it.

---

## WHY THE SYSTEM CAN'T FIND TRADES RIGHT NOW  ← (the direct question)

Two reasons, one shallow and one deep:

**1. Over-gating (shallow).** `india_swing` runs ~11 sequential gates
(G0 regime → G1 trend stack → G2 pullback → G3 reversal candle + vol≥2× →
G4 structural stop → G5 RSI/RS → G6 sector → G7 earnings → G8 delivery →
G9 sector-leader top-3 → G10 ML). Each gate alone passes maybe 30–60% of the
time. **In series they multiply.** If 11 gates each pass ~35%, the combined
pass rate is `0.35^11 ≈ 0.0000018` — essentially zero. Measured directly:
- `PRECISION_MODE=1` alone (live default): **~2 trades / 30 names / 2 years**.
- Full live parity (PRECISION + G9 + G10 + entry_guard + finalize): **0 trades**.
- Loosen to the permissive config: ~22 trades / 30 names / 2yr — but **PF 0.50**.

(Note: the once-per-day evaluation fix — `config.ISWING_DECISION_TIME` — is
*correct* behaviour for a daily-close swing strategy and is NOT the cause; it
just stops the old 15s re-scan churn. The gate stack is the cause.)

**2. No edge (deep, and the real reason).** Every one of those gates was
*curve-fit* to remove past losers. With **no real edge**, tightening filters to
raise win-rate just chokes off trades without creating profitability. An
edgeless system, tightened honestly, **converges to ~zero trades** — because
there is no reliably-profitable subset of setups to find. So "the system can't
find trades" is, paradoxically, the system **correctly reporting that there is
nothing reliably tradeable here.** Loose settings pass junk (PF<1); tight
settings pass nothing. There is no middle setting that passes a profitable
subset, because that subset doesn't exist in this data.

**Implication:** don't "fix" the trade scarcity by loosening gates — that just
restores negative-expectancy trading. The scarcity is a symptom, not the disease.

---

## What was tested (and the honest result)

| Approach | Result | Why it fails |
|---|---|---|
| Committed `india_swing` backtest | PF **0.72**, −0.54%/trade | net-negative after cost |
| Gap-honest live-parity backtest | PF **0.50** (permissive); 0 trades (live) | cost + over-gating |
| Live journal (`signals.json`) | 100% LEGACY engine, options-premium | `PF 16` = premium-% artifact |
| Trend (close>SMA200) | **dead** — buy&hold beat it 12% vs 1% | no single-market trend edge |
| Cross-sectional momentum | **negative** both halves | — |
| Mean-reversion (RSI-2) | real gross PF 1.19, dies ~0.20% cost | transaction cost |
| Overnight drift | **real** (+0.061%/day) | −10%/yr after daily-trade cost |
| VIX-spike reversion | bull-only; **−0.10%/trade thru COVID** | regime/tail risk |
| VRP (India VIX − realized) | **real**, weekly t=2.67 | OOS not sig; brutal tail |
| Iron condor (tradeable VRP) | **−1.8%/trade** net | tail-protection cost eats premium |
| **Pairs / stat-arb (best result)** | OOS PF 1.53, **maxDD −2.7%**, Sharpe 0.60 | OOS halves −0.50/+1.37 = unstable |

**Theme:** every real signal is offset by its harvesting cost or tail/regime
risk — the signature of a reasonably efficient market.

---

## Tools built (honest research apparatus — reuse these)

| File | Purpose | Run |
|---|---|---|
| `honest_metrics.py` | Re-score the live journal honestly (excludes premium mirage) | `python honest_metrics.py` |
| `backtest_live_pipeline.py` | Gap-honest, point-in-time, live-parity backtest of the real strategy | `--selftest` (offline), or `--full` |
| `edge_research.py` | Momentum / trend / mean-rev battery vs NIFTY | `python edge_research.py --days 1095` |
| `validate_meanrev.py` | RSI-2 mean-reversion, real OHLC, **cost sweep** | `python validate_meanrev.py --full` |
| `validate_meanrev_liquid.py` | Mean-rev by liquidity tier | `--full --limit 80` |
| `edge_hunt.py` | Broad 7-hypothesis battery, multiple-testing aware | `--full --limit 80` |
| `pairs_test.py` | Pairs trading, IS-select / OOS-trade | `--full --limit 150` |
| `pairs_program.py` | **Diversified market-neutral stat-arb program** (cointegration, portfolio) | `--full --limit 150 --days 1460` |
| `research_vrp.py` | VRP spread (Newey-West HAC, IS/OOS, tail) — *pre-existing, fixed unicode* | `python research_vrp.py` |
| `research_iron_condor.py` | Defined-risk VRP vehicle (BS-priced) — *pre-existing, fixed unicode* | `python research_iron_condor.py` |

All emit `--selftest`-style honesty: fixed params, OOS split, cost sweep, and a
verdict that can say "no edge".

---

## Honest fixes applied (plumbing — verified, no edge faked)

| File | Fix |
|---|---|
| `core/metrics_writer.py` | PF/DD now computed on TRUSTWORTHY directional pnl; premium rows excluded; drift alarm **can fire** (PF<0.9, PF>3 implausible, DD, or <20 clean trades). Was blind (PF~16 never tripped). |
| `core/risk_engine.py` | `check_daily_loss(mark_prices)` includes OPEN MTM; new `check_correlation` sector cap; both wired into `can_trade`. |
| `config.py` | `LEARNING_ENABLED=False` + `AUTO_REPAIR enabled=False` (froze in-session overfitting); `RISK_CONFIG.max_per_sector=2`; `REQUIRE_EARNINGS_DATA`; `ISWING_DECISION_TIME`. |
| `scan_only_v2.py` | In-session learner gated off; removed false "Grade-S ~75-80% WR" label; `india_swing` evaluated once/day (not every 15s on a partial bar). |
| `core/earnings_calendar.py` | `is_blackout` fails CLOSED when `REQUIRE_EARNINGS_DATA=1`. |

---

## Ratings

| Dimension | Before | After fixes |
|---|---|---|
| Edge strength | 1 | **2** (real signals, none exploitable) |
| Robustness | 1 | 4 |
| Risk control | 3 | 6 |
| Execution realism | 2 | 4 |
| Production readiness | 1 | 5 |
| Measurement honesty | 2 | 7 |
| **Overall (as a money-maker)** | **2** | **4.0** |

---

## Path forward

1. **Capital:** money needed within a year → safe & liquid (NOT the edge hunt).
2. **Capture the real edge cheaply:** index fund core (Nifty 50/500) + factor
   ETFs (Momentum 30, Value 20, Low-Vol 30) via Dhan. Direct plans, low cost.
   Pledge liquid ETFs as F&O collateral (your capital does double duty).
3. **Keep `pairs_program.py` as a PAPER research project** — best fit for the
   builder profile, lowest drawdown; only consider tiny capital if a longer /
   multi-regime test ever shows stable Sharpe>1 in BOTH held-out halves.
4. **Do NOT loosen the gates to "find more trades."** Scarcity = no edge, not a bug.

## Discipline rules for any future edge work (so we don't overfit again)
- Fixed, a-priori params. No tuning to make a backtest pass.
- Out-of-sample / held-out split is the only result that counts.
- Net of realistic costs (assume higher than you think). Cost sweep everything.
- Survivorship: prefer point-in-time universes incl. delisted names.
- Multiple testing: N hypotheses → ~N×5% false positives by chance; demand OOS.
- Name the counterparty: "who loses to me and why?" If you can't, it's not an edge.
- A real edge is small, repeatable, survivable, compounded — never the 15000% home run.
