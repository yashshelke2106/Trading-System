# Pullback — single-setup mode

The one setup the data backs. Everything else is OFF.

## The setup

**Trend pullback continuation, longs only, regime-gated.**

1. **Trend** — daily EMA20 > EMA50, EMA20 sloping up (gate1)
2. **Pullback** — price dipped to EMA20 and *rejected* (closed back above), OR compressed base; not extended (gate2)
3. **Confirmation** — reversal candle (engulfing / marubozu / pin) at the pullback (gate3)
4. **Longs only** — `ONLY_LONG=1`. Short WR was 12% (n=8); NSE stocks grind up, crash rare+fast. Short edge needs a different setup.
5. **Regime gate** — scan skips entirely when NIFTY ADX < 20 (chop) or earnings cluster (`core/regime_filter.py`)
6. **Risk** — SL below pullback swing low (tight ~1-1.5%), target 2R, breakeven trail at 0.7R
7. **Re-entry block** — a symbol that hit SL today is locked out till next session
8. **Instrument: stock FUTURES, not options** (`INSTRUMENT_MODE=futures`)
9. **Universe filter** — block stocks where the setup is a proven spot loser

## Instrument: why futures, not options

The backtested edge (PF 1.17) was measured on **spot prices**. The first 16
live days traded **options**, and the journal (911 trades) shows the
mismatch tax:

| Outcome | SPOT moved | OPTION premium did |
|---------|-----------|--------------------|
| SL_HIT  | -0.60% (median) | **-9.56%** |
| TARGET  | +0.66% | +21.67% |

On stop-losses the stock barely moved — the **option premium** decayed
(theta + IV crush + spread). **15% of trades had the direction RIGHT yet the
option still lost.** And per-symbol, names that looked like losers on options
were winners on spot (PAYTM option 2/10 → spot 10/10; PFC 1/9 → spot
positive every time).

Conclusion: the edge is in price direction; options pollute it. Stock
**futures** (delta ~1, no theta decay, tight spread on liquid names) express
the same view cleanly. `INSTRUMENT_MODE=futures` is now the default; the
scanner attaches a futures leg (lot size, spot-level entry/SL/target) and
skips option-chain enrichment entirely.

## Universe filter

`core/universe_filter.py` ranks every F&O symbol by how the setup performed
on its **spot** move in the journal. Chronic spot losers are blocked
(currently NHPC, NAVINFLUOR, NATIONALUM — negative expectancy over n>=5);
untested symbols pass (innocent until proven). One strategy, filtered
universe — NOT a per-stock strategy (that overfits). Rebuild with
`python -m core.universe_filter`.

## Why this and nothing else

Measured this session (2yr / 30-stock backtest, G9/G10/regime off for apples-to-apples):

| Config | Trades | WR | PF | Expectancy |
|--------|--------|-----|------|-----------|
| india_swing mixed (longs+shorts) | 57 | 25% | 0.94 | -0.00R |
| **pullback longs-only** | 50 | 30% | **1.17** | **+0.10R** |
| ORB | 538 | 40% | 0.63 | -0.15R |

Cutting shorts flipped PF 0.94 → 1.17 — the first PF > 1 of the whole build.

Grade split (longs-only): **A = 15% WR / -0.17R (noise), B = 41% WR / +0.32R (the edge).**
The edge lives in B-grade. A-grade is over-confident and bleeds — candidate to drop next.

## What's OFF and why

- **Shorts** — negative edge on NSE stocks
- **ORB** — PF 0.63, 65% EOD exits, intraday range dies in chop
- **Volatility expansion** — unmeasurable (no intraday option premium data)
- **Legacy 17-detector vote stack** — 30% WR, no grade edge (set `STRATEGY_LEGACY=1` only to A/B compare)

## Run it

```
start_pullback.bat     # scanner (longs-only) + tracker + API + UI, PAPER_TRADE locked
stop_trading.bat       # shut all down
```

Env it sets: `ONLY_LONG=1`, `PRECISION_MODE=0`, `PAPER_TRADE=1`. Regime gate + G9/G10 stay live.

## Forward-test protocol (do not skip)

1. Run **daily for 4 weeks**. Tracker journals every signal → `logs/signal_journal.jsonl`; metrics roll up at 15:31 → `logs/metrics_daily.jsonl`.
2. **Do NOT tune mid-test.** The backtest edge is in-sample; forward data is the only honest verdict. Touching params resets the clock.
3. Watch `drift_alert` in metrics. 
4. **Decision after ≥ 30 forward trades:**
   - 30d PF < 1.0 → **kill it.** Don't tune — the setup has no forward edge. Try a different market/timeframe.
   - 30d PF 1.0–1.2 → extend test another 4 weeks.
   - 30d PF > 1.2 → **promote.** Size up slowly (fixed 1% risk/trade).

## Next code levers (only AFTER forward data)

- Drop A-grade (15% WR) → B-grade only. Likely lifts PF further.
- Retrain G10 ML on the live journal (not the backtest CSV — that was circular).
- Run `walk_forward_fit.py` monthly to re-fit thresholds on rolling window.
