# PROJECT EXPLAINED — Complete Interview Reference

Everything in this project: every logic, library, strategy, algorithm, and the
reasoning behind it. Study this and you can explain the project end-to-end.

---

## 1. THE 30-SECOND ELEVATOR PITCH

> "I built an algorithmic trading research platform for Indian F&O (futures &
> options) markets — Python backend, Next.js dashboard, live broker API
> integration. It scans ~150 NSE stocks in real time, generates signals through
> a multi-gate strategy pipeline, manages risk, and paper-trades. The most
> interesting part: I built a rigorous validation layer (out-of-sample testing,
> realistic cost modeling, gap-honest fills) that ended up **falsifying ~20 of
> my own strategies** — and proving one options-income strategy genuinely beat
> the benchmark. The project taught me more about honest measurement and
> statistical rigor than about chart patterns — which is exactly the point."

The honest arc is your strongest interview story: *built a complex system →
discovered its metrics were lying → rebuilt measurement honestly → falsified
the strategies → found one real edge → engineered safe capture of it.*

---

## 2. TECH STACK & LIBRARIES (and WHY each)

### Backend (Python 3.12)
| Library | Used for | Why this one |
|---|---|---|
| **pandas** | All OHLCV time-series: rolling windows, resampling, joins | Industry standard for financial data frames |
| **numpy** | Vectorized math: indicator arrays, P&L vectors, equity curves | 10-100x faster than Python loops on price arrays |
| **scikit-learn** | LogisticRegression + GradientBoosting (ML signal filter), StandardScaler | Simple, interpretable models; small datasets (~200-1000 trades) would overfit anything deeper |
| **scipy** | Statistical tests (t-tests for turn-of-month effect), norm CDF | Standard statistics |
| **statsmodels** | ADF (Augmented Dickey-Fuller) test for pairs cointegration | The canonical stationarity test |
| **requests** | Dhan broker REST API (HTTP), NSE public endpoints | With urllib3 Retry adapters for resilience |
| **websockets** | Dhan live market feed (real-time ticks) | Async push beats polling for live prices |
| **FastAPI + uvicorn** | REST + WebSocket API server (:8000) feeding the UI | Async, typed, auto-docs; lighter than Django |
| **streamlit** | Legacy analytics dashboard | Fastest way to prototype data dashboards |
| **keyring** | OS-level credential store for API secrets | Never hardcode secrets; falls back to env/files |
| **pytest** | 80-test suite guarding money-math and safety flags | Standard; fixtures + monkeypatch for isolation |
| **anthropic** | Optional LLM-RAG over the trade journal | Natural-language querying of trade history |

### Frontend
| Tech | Used for |
|---|---|
| **Next.js 16 + React 19** | Live trading dashboard (:3000), polls state.json / WebSocket |
| **Tailwind CSS 4** | Styling |
| **TypeScript** | Type safety in the UI |

### Data sources
- **Dhan API** (paid): historical OHLCV (`/charts`), option chains, live feed.
- **NSE public API**: option-chain fallback, F&O universe validation.
- **India VIX**: implied-volatility index — input to options pricing/research.

---

## 3. ARCHITECTURE & DATA FLOW

```
                      ┌──────────────────────────────────────────┐
   Dhan REST/WS ────► │ DATA LAYER (core/api_dhan.py)            │
   NSE public  ────►  │  caching · throttling · dedup · failover │
                      └────────────────┬─────────────────────────┘
                                       ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ SCANNER (scan_only_v2.py / aladdin_runner.py)                │
   │  universe validation → strategy pipeline → enrichment        │
   │  → finalize/rank → signals.json (NO real orders)             │
   └────────────────┬─────────────────────────────────────────────┘
                    ▼
   ┌────────────────────────────┐   ┌──────────────────────────────┐
   │ TRACKERS                   │   │ UI                           │
   │  signal_tracker (outcomes) │   │  FastAPI :8000 → Next.js     │
   │  covered_call_tracker      │   │  :3000 + Streamlit legacy    │
   │  → journal JSONL           │   └──────────────────────────────┘
   └────────────────┬───────────┘
                    ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ MEASUREMENT & VALIDATION (the crown jewel)                   │
   │  metrics_writer (honest PF/DD + alarms) · honest_metrics     │
   │  backtest_live_pipeline (gap-honest, point-in-time)          │
   │  edge_hunt / bakeoffs / pairs_program / VRP harnesses        │
   └──────────────────────────────────────────────────────────────┘
```

**Key design decisions to explain:**
- **Signal-only mode**: scanner writes `signals.json`; execution is manual /
  paper (`PAPER_TRADE=True`). Separation of signal generation from execution.
- **Append-only JSONL journals**: every signal and outcome is an immutable
  log line — auditability, easy pandas ingestion, crash-safe appends.
- **File-based IPC**: processes communicate via JSON files (state.json,
  signals.json) — simple, debuggable, no message-broker dependency.
- **Idempotency everywhere**: journal dedupes by (symbol, direction, date);
  metrics writer is idempotent per day; CC tracker holds one cycle at a time.

---

## 4. DATA-LAYER ENGINEERING (production problems I solved)

1. **Rate limiting (HTTP 429)**: Dhan allows ~5 req/s. Solutions:
   - Per-endpoint client-side throttle (1 req/s on `/charts`).
   - **Session-aware daily cache**: daily bars can only change at market open
     and close, so cache expires at those boundaries (~2 fetches/symbol/day
     instead of hourly) — removed ~90% of historical-data load.
   - **Cross-process shared backoff**: backoff state in a JSON file so ALL
     processes honor one 429 (a class attribute is process-local — multiple
     processes each tripped their own backoff while others kept hammering).
2. **Duplicate data**: Dhan occasionally returns duplicate-date rows →
   `drop_duplicates(subset="date")` at the fetch layer (downstream
   `reindex` crashes on duplicate labels otherwise).
3. **Token lifecycle**: JWT expires daily; `check_token_health()` validates at
   startup; keyring-first secret storage.
4. **Staleness halt**: if Dhan goes silent mid-session, the scanner refuses to
   publish signals built on stale data (fail-closed, with alert).
5. **Scrip master**: 30MB CSV of every NSE instrument, cached 24h, indexed
   into dicts for O(1) security-ID and **live lot-size** lookup (hardcoded lot
   sizes go stale when NSE revises them — caused real position-sizing errors).

---

## 5. STRATEGIES — every one, with its logic and its fate

### 5.1 Legacy "17-detector vote stack" (core/signal_engine.py) — RETIRED
Pattern detectors each cast long/short votes; signal fires on ≥5 votes with
≥3-vote lead. Detectors: EMA 9/21 crossover (+EMA8 stack alignment), VWAP
breakout, RSI zones ladder, NR7 (narrowest range in 7 bars), inside-bar
breakout, EMA21 pullback, Supertrend (ATR bands w/ Wilder smoothing),
candlestick patterns (pin bar / engulfing), consolidation breakout, bull/bear
flag (impulse→volume-dry-up→break), multi-touch horizontal levels,
supply/demand zones (swing pivots ± ATR buffer), broken-zone retest (MTF),
PVSRA volume bars, Waddah Attar Explosion (MACD momentum vs Bollinger width),
smoothed Range Filter. **Fate: 30% live win rate, no grade predictiveness —
votes correlate (they all measure trend), stacking added no information.**

### 5.2 india_swing — sequential gate strategy (core/strategy_india_swing.py)
Replaced voting with **sequential binary gates** (short-circuit on first fail):
- **G0 Regime**: NIFTY vs EMA50 + slope — no longs in bearish market.
- **G1 HTF Trend**: close > EMA20 > EMA50, EMA20 slope > 0 (daily).
- **G2 Pullback**: touched EMA20 with rejection close, or 3-bar compression
  (avg body ≤ 0.7×ATR); reject extended candles (chasing).
- **G3 Confirmation**: reversal candle (engulfing/marubozu/pin) + volume ≥2×
  20-day average + close in top 30% of range.
- **G4 Structural risk**: stop = swing-low pivot − 0.5×ATR buffer; risk capped
  4%; target = entry + 1.5R. No valid pivot → no trade.
- **G5 Quality**: RSI in zone (50-65 long), relative strength vs NIFTY ≥1.05.
- **G6 Sector alignment, G7 earnings blackout, G8 delivery %, G9 sector-leader
  rank (top-3 by 20d return, point-in-time), G10 ML probability filter.**
**Fate: backtest PF 0.72 (net-negative); honest gap-fill re-test PF 0.50;
full live-parity config produced ~0 trades — the gates correctly choked an
edgeless strategy to silence.**

### 5.3 Others built & tested
- **ORB** (opening range breakout, 9:45-12:00 window) — intraday.
- **Volatility expansion** (compression → straddle/strangle on option chain).
- **5 "true breakout" variants** (Donchian-20, +volume filter, squeeze,
  52w-high, breakout-retest) — **all five negative OOS** (~3,400 trades;
  PF 0.64-0.82). Breakouts = leveraged market beta, filters didn't help.
- **Cross-sectional momentum (12-1)**, **SMA200 trend filter**, **Connors
  RSI-2 mean reversion**, **low-volatility quintile**, **turn-of-month**,
  **overnight drift**, **VIX-spike reversion**, **pairs/stat-arb** — all run
  through the same gauntlet (see §7 for results).
- **VRP research**: India VIX vs subsequent realized vol (the premium is real,
  t=2.67 weekly) and an **iron-condor harness** (4 BS-priced legs — negative
  net: wing cost eats the premium).

### 5.4 THE SURVIVOR: Covered Call on NIFTY (covered_call_test.py + tracker)
Hold NIFTY (ETF) + sell monthly 2.5%-OTM call (50-pt strike grid), priced with
Black-Scholes @ India VIX, 5% premium cost, settle vs actual NIFTY at expiry.
**Result: Sharpe 0.99 vs buy-and-hold 0.72, beat B&H in BOTH halves of
2019-2026 incl. COVID; all three strike variants improved Sharpe — the
signature of a real effect, consistent with decades of BXM buy-write
literature. Why it works where the condor failed: the shares ARE the hedge —
no wing cost.** Now in a live paper-validation loop (covered_call_tracker.py)
comparing realized market premium vs model (`premium_ratio`; ≥0.8 after 2-3
cycles = model honest; first live reading: 0.797).

---

## 6. CORE ALGORITHMS & MATH (be ready to whiteboard these)

### Indicators (computed from scratch, no TA-lib)
- **RSI**: `100 - 100/(1+RS)`, RS = avg gain / avg loss over N (Wilder).
- **ATR**: rolling mean of True Range = max(H-L, |H-prevC|, |L-prevC|).
- **EMA**: `ewm(span=N)`; **VWAP**: Σ(typical price × vol)/Σvol, session-reset.
- **Supertrend**: bands at HL2 ± mult×ATR with ratcheting upper/lower logic.
- **OU half-life** (pairs): regress Δspread on lagged spread; HL = −ln2/β.

### Risk engine math (core/risk_engine.py)
- **Position size**: `qty = (capital × risk_pct) / |entry − stop|`, rounded
  down to exchange lot size (risk-based sizing, not notional-based).
- **Stop**: ATR-based (1.2×ATR), clamped to [1%, 2.5%] of entry.
- **Daily kill-switch**: realized + **open MTM** loss ≥ 4% capital → halt.
- **Correlation cap**: max 2 simultaneous positions per sector.
- **Consecutive-loss halt** (2), **max trades/day** (3), per-symbol same-day
  re-entry block after a stop-out.

### Options math
- **Black-Scholes call**: `C = S·N(d1) − K·e^(−rT)·N(d2)`,
  `d1 = [ln(S/K) + (r+σ²/2)T] / σ√T` — used for condor legs + covered-call
  premium with India VIX as σ, r = 6.5%.
- **Covered-call cycle return**: `(S_T−S_0)/S_0 + (premium − max(S_T−K,0))/S_0`.
- **VRP**: `IndiaVIX_t − realized_vol(t, t+h)` where realized = annualized
  stdev of daily log returns; significance via **Newey-West HAC** standard
  errors (overlapping windows are autocorrelated — naive t-stats overstate).

### Pairs / stat-arb (pairs_program.py)
- Pre-screen: return correlation > 0.6.
- **Cointegration**: regress log(A) on log(B) → hedge ratio β; ADF test on
  residual spread (p < 0.05); half-life filter 3-40 days.
- Trade: z-score of spread (rolling 30d); enter |z|>2, exit |z|<0.5, stop
  |z|>3.5; market-neutral (long one leg, short the other, β-weighted).

### Signal ranking (core/trade_ranker.py)
Weighted composite (weights sum to 1): signal strength 0.23, order flow 0.18,
breakout quality 0.16, liquidity 0.13, market bias 0.10, sector momentum 0.10,
volatility 0.05, time 0.05 → verdict tiers (EXCELLENT/GOOD/AVERAGE/AVOID).

---

## 7. THE VALIDATION METHODOLOGY (your strongest interview material)

The original system's metrics showed **PF ~16, 50% WR** — and it was losing
money. Root cause: the metric summed **option-premium percentage moves**
(theta/IV-dominated, don't compound) and the drift alarm fired at PF<0.9 —
mathematically unable to trigger on a metric pinned at 16. **The monitoring was
structurally blind.** Fixing this honestly became the project's core:

1. **Honest P&L attribution**: spot/futures directional pnl only; option-
   premium rows excluded from PF; alarm now ALSO fires on PF>3 ("impossibly
   good = your metric is broken").
2. **Gap-honest fills**: if a bar OPENS beyond the stop, fill at the open, not
   the stop price (Indian stocks gap on news; the old backtest hid +1.2R of
   losses this way). Intrabar: stop checked before target (conservative).
3. **Point-in-time discipline**: every gate computable as-of the signal date
   (no look-ahead); sector rankings reconstructed historically.
4. **One code path**: the backtest calls the SAME strategy function the live
   scanner runs (the old system backtested a different pipeline than it ran).
5. **Out-of-sample splits**: fixed a-priori parameters, train/selection on the
   first 60%, verdict ONLY on the held-out 40%; for pairs, selection on IS,
   trading blind on OOS.
6. **Cost sweeps**: every strategy evaluated at multiple round-trip costs —
   e.g. RSI-2 mean reversion is PF 1.13 at 0.06% cost and 0.99 at 0.20%:
   *the same signal is an edge or a loser depending on execution cost.*
7. **Survivorship awareness**: today's F&O list inflates results; flagged on
   every report; negative results are decisive, positives get discounted.
8. **Multiple-testing control**: N hypotheses ⇒ ~N×5% false positives by
   chance; a single "pass" is a lead to re-test, never a green light.
9. **Pre-committed decision rules**: e.g. covered-call `premium_ratio ≥ 0.8`
   threshold fixed BEFORE the data arrives — no renegotiating with results.

**Falsified by this process** (each with hundreds-thousands of OOS trades):
all 5 breakout variants, trend filter (buy&hold beat it 12% vs 1% annualized),
cross-sectional momentum, mean reversion (cost-killed), low-vol (unstable),
turn-of-month (p=0.70), overnight drift (real signal, −10%/yr after daily
costs), VIX-spike reversion (bull artifact; −0.75%/trade through COVID),
iron condor (−1.8%/trade), 17-detector stack, india_swing.
**Survived: covered call (Sharpe 0.99 vs 0.72, both halves).**

---

## 8. ENGINEERING PRACTICES (software-quality talking points)

- **80-test pytest suite**: risk math, honest metrics, gap-honest exit
  simulator, indicators, API throttle/backoff, **config invariants** (a test
  FAILS if someone flips PAPER_TRADE, breaks lot sizes, or re-enables the
  in-session learner — safety flags are regression-tested).
- **Fail-closed design**: strategy gates that error can block the trade
  (GATES_FAIL_CLOSED); data staleness halts publishing; earnings gate can
  fail-closed when its feed is down.
- **Self-tests in research tools**: `--selftest` verifies exit mechanics /
  tracker math offline before touching real data.
- **Frozen parameters**: no runtime self-tuning (the in-session "adaptive
  learner" was continuously overfitting to its own journal — disabled, gated
  behind a config flag).
- **ML hygiene**: temporal (not random) train/test splits; removed a
  train/serve skew (a feature constant in training but live at inference);
  schema-versioned model artifacts; cold-start pass-through.
- **Idempotent daily jobs**, atomic file writes (tmp + os.replace), Windows
  Unicode hardening (UTF-8 stdout), graceful degradation on every external
  dependency.

---

## 9. RESULTS SUMMARY (the honest numbers)

| What | Number |
|---|---|
| Strategies/hypotheses tested through the gauntlet | ~20 |
| Falsified (no OOS, cost-surviving edge) | ~19 |
| Survived: covered call vs buy&hold | Sharpe **0.99 vs 0.72**, both halves, 7yr incl COVID |
| The broken metric exposed | "PF 16" → real strategy PF **0.72** |
| Test suite | **80 passed** |
| Live mode | Paper-only; covered-call sleeve in forward validation |

---

## 10. INTERVIEW Q&A — likely questions, strong answers

**Q: Did it make money?**
"It's in paper mode by design. The honest answer is the project's best result:
my validation layer proved my directional strategies had no edge BEFORE I
scaled real capital into them, and identified one options-income strategy that
genuinely beats the benchmark — now in forward validation. In trading,
preventing losses on false edges IS the win; most retail systems never build
the measurement to know the difference."

**Q: What was the hardest bug?**
"My monitoring said PF 16 while the system lost money. The metric summed
option-premium percentages — which are theta-dominated and don't compound —
and the 'performance degraded' alarm fired at PF<0.9, so it could never
trigger. I rebuilt attribution on directional P&L and added an
'implausibly-good' alarm: PF>3 now also fires, because a too-good metric means
the metric is broken. Lesson: monitoring must be able to detect its own lies."

**Q: Why did your strategies fail?**
"Three measured reasons: transaction costs (real signals like overnight drift
and short-term reversal exist but die at retail round-trip costs — I showed
the same signal flips from PF 1.13 to 0.99 between 0.06% and 0.20% cost);
regime dependence (breakouts were just leveraged market beta — in-sample
PF 1.29 became 0.66 out-of-sample when the market turned); and overfitting
(parameters tuned on 30 stocks reversed sign on 152). That's why the
validation layer exists."

**Q: What would you do differently?**
"Build the measurement layer FIRST. I built 90 strategy modules on top of a
broken scale. Truth-first architecture: journal → validation harness → monitor
with alarms → and only then strategies, as plugins that must pass the
apparatus to exist."

**Q: How do you prevent look-ahead bias?**
"Every gate takes an as-of date and uses only data ≤ that date; fills happen
at the NEXT bar's open; sector rankings are reconstructed point-in-time;
pairs are selected on the first 60% and traded blind on the rest; and the
backtest calls the same function the live scanner calls."

**Q: Scale/perf?**
"150-symbol scans against a rate-limited API: session-aware caching cut daily
re-fetches ~90%, cross-process shared backoff stopped 429 storms, vectorized
numpy for indicator math, O(1) scrip-master lookups from a 30MB CSV."

**Q: What's the one-line lesson?**
"A strategy is a machine; an edge is whether the machine's output is worth
more than its fuel. Accuracy decides how much of an edge you keep — it can't
decide whether there is one. I built the accuracy, and let it tell me the
truth about the edge."

---

## 11. FILE MAP (where everything lives)

| Area | Files |
|---|---|
| Entry points | `scan_only_v2.py`, `aladdin_runner.py`, `api_server.py`, `signal_tracker.py` |
| Strategy | `core/strategy_india_swing.py`, `core/signal_engine.py`, `core/orb_strategy.py`, `core/volatility_strategy.py` |
| Risk | `core/risk_engine.py`, `core/entry_guard.py`, `core/signal_finalize.py` |
| Data | `core/api_dhan.py`, `core/scrip_master.py`, `core/nse_option_chain.py`, `core/nse_calendar.py` |
| Measurement | `core/metrics_writer.py`, `honest_metrics.py`, `core/signal_journal.py` |
| Validation | `backtest_live_pipeline.py`, `edge_hunt.py`, `strategy_bakeoff.py`, `breakout_bakeoff.py`, `validate_meanrev*.py`, `pairs_program.py`, `research_vrp.py`, `research_iron_condor.py` |
| The survivor | `covered_call_test.py`, `covered_call_tracker.py` |
| Tests | `tests/` (80 tests) |
| Docs | `AUDIT_AND_EDGE_HUNT.md`, `OPERATING_MANUAL.md`, this file |
