# Factor findings — what the real P&L says

Analysis of the **919-trade live journal** (2026-05-05 → 2026-05-29), judged on
the **spot move** (the true directional outcome — option premium is polluted
by theta/IV and we trade futures now). Re-run any time:

```
python analyze_factors.py                    # live journal
python analyze_factors.py --csv backtest_india_swing_trades.csv
```

## Method that keeps us honest

Every factor is measured **within each direction**. The journal window was a
**downtrend** (NIFTY −2.7%), so across the whole sample shorts look 81% and
longs 48% — that is the *market falling*, not an edge. A factor is only real
if it separates winners from losers **inside** a direction (trend held
constant) **and** has a mechanism.

## What was WRONG — removed / fixed (regime-independent)

| Finding | Evidence | Action |
|---|---|---|
| **Grade does not predict outcome — it's inverted** | Grade **S** (sized largest, 1.0) = **11-25% WR**, the WORST bucket in both directions. Grade A = 50-68%. | **Removed grade-based position sizing** in `scan_only_v2`. Size is now by **R:R** (mechanical, real): base 0.5, +0.25 per R above 1.5, cap 1.0. Grade/score affect display only, never size. |
| **confluence_score is non-monotonic with outcome** | Long score **130+ = 33% WR** (worst); 80-100 = 54%. Higher score ≠ better. | Score no longer drives size (same fix). It stays as a display/rank hint only. |
| **Breakout-chasing patterns are loss-magnets** | Longs: `bull_flag_breakout` **21% WR** (-26pt), `horizontal_breakout_up` 33%, `supply_zone_bos_up` 33%. | Added `bull_flag_breakout_chase` + `horizontal_breakout_chase` to `LOSS_MAGNETS` in `core/setup_detector.py` (these KILL the signal). Consistent with v1 (breakout_5d = 54% of losers) and ORB PF 0.63. |
| **vote_margin is noise** | Long 11+ votes = 40% WR (worse than ≤7). | Not used for sizing/ranking. Confirms the move away from vote-stacking to gates was correct. |

## What is REGIME-DEPENDENT — deliberately NOT acted on

These showed up strongly but are artifacts of one down-month. Hardcoding them
would be curve-fitting (the exact trap that kept v1→v4 PF flat):

- **"Shorts win 81%, longs 48%"** — only because the market fell. Do NOT flip
  to shorts-only.
- **"RSI <40-50 wins"** — partly mean-reversion (real), partly "everything
  oversold bounced in a falling market." The within-long signal was weak
  (RSI 50-70+ all ~46-50%). Keep the existing precision-mode RSI band; do not
  over-tighten on this window alone.

## Important caveat about the dataset

The 919 journal trades are **mostly the legacy 17-detector vote-stack engine**
(patterns like `wae_bull_explosion`, `pvsra_super_bull`, `rsi_momentum_zone`),
because the journal predates the 2026-05-26 switch to `india_swing`. So this
analysis:
- **Confirms the legacy engine is noise** (grade inverted, breakouts bleed,
  score non-predictive) — it is already off by default (`STRATEGY_LEGACY=1`
  to opt in).
- Does **not** yet validate or refute `india_swing` itself (too few of its
  trades here). The clean test for the current strategy remains the
  **full-universe 2-year backtest** across multiple regimes
  (`backtest_india_swing.py --full`), then re-run `analyze_factors.py --csv`
  on that output.

## Round 2 — india_swing's OWN data (230-trade, 2yr, multi-regime)

Pulled the 230-trade india_swing backtest from git history (commit AL) and
ran `analyze_factors.py --csv` on it. Unlike the journal, this IS the current
strategy across 2 years and multiple regimes — the clean test. Acted only on
findings that ALSO hold in the journal (cross-validated, mechanism-backed):

| Finding | Backtest (2yr) | Journal (1mo) | Action |
|---|---|---|---|
| **Mid-RSI longs win, overbought longs lose** | RSI 50-60 = **55% WR**, 60-70 = 40%, 70+ = 38% | RSI 40-50 best, 60-70+ = 46% | **RSI_LONG_MAX 65 → 60** in `strategy_india_swing` precision mode. Mechanism: don't buy overbought. |
| **Lone pin-bar longs bleed; strong-body candles win** | `bullish_pin_bar` **27% WR** vs `bullish_marubozu` **55%**, engulfing 50% | (pin bars weak in legacy too) | gate3: a **lone** `bullish_pin_bar` long now needs a real volume surge (≥1.25× the floor) to qualify; engulfing/marubozu qualify as before. Mechanism: indecision candle vs commitment candle. |

Both changes are cross-validated (two independent samples) and mechanistic, so
they are NOT curve-fits to one window.

Still NOT acted on (single-sample / weak): grade S marginal +3pt for longs in
the backtest but worst in the journal → net non-predictive, kept out of sizing;
short-side score/grade quirks (n too small).

## Round 3 — the full-universe verdict (and it overturned Round 2)

Ran the decisive test: **152-stock, 2-year, real-Dhan** backtest (146 long
trades), then `analyze_factors.py` on it. This is the largest, freshest,
multi-regime sample we have — it outranks the 30-stock and 1-month samples.

**Strategy result: WR 23%, PF 0.72, expectancy -0.12R.** No edge.

**It DISPROVED the Round-2 tunes — which were small-sample curve-fits:**

| Round-2 claim (30 stocks) | Round-3 reality (152 stocks) | Action |
|---|---|---|
| RSI 50-60 = 55% WR → cut ceiling to 60 | RSI 50-60 = **26%**, 60-70 = 23%, 70+ = 25% — flat, no edge | **REVERTED** RSI_LONG_MAX to 65 |
| pin-bar 27% (worst) → demote | pin-bar **29% (best)**, marubozu **21% (worst)** — opposite | **REVERTED** the pin-bar volume gate |

Lesson logged honestly: within-direction discipline + journal cross-check was
NOT enough — both small samples were biased the same way, so the overfit
survived the cross-check and only died on the full universe. **Trust the
largest multi-regime sample; treat anything found on <100 trades as a
hypothesis, not a fact.**

**What SURVIVED Round 3 (still valid on the full sample):**
- Grade-based sizing removal. On 146 trades grade is STILL non-predictive
  (A 24%, B 24%, S 25%). R:R sizing stays.
- The reusable analyzer (it did its job — it's what caught the overfit).

## Bottom line — the hard truth

On 146 trades across 2 years and 152 stocks on real data, **no measured factor
(RSI, grade, score, pattern, volume) separates winners from losers**, and the
strategy is a net loser (PF 0.72, 23% WR). That is not a tuning problem — it
means the **entry signal carries no directional information**. Every earlier
PF>1 (the 30-stock 1.17, the 230-trade run) was small-sample luck.

You cannot tune your way to an edge when nothing discriminates. The honest
next step is NOT another factor tweak — it is either a fundamentally different
signal (different timeframe / mean-reversion / event-driven), or accepting
that simple daily-bar pullback longs on NSE F&O have no edge after costs. More
parameter fiddling on this entry = more curve-fitting.
