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

## Bottom line

Removed two genuinely harmful, regime-independent things: **grade-based sizing
that amplified the worst trades**, and **breakout-chase loss-magnets**. Added
**R:R-based sizing** (a real factor) and a **reusable analyzer** so every
future cut/add is data-backed and re-checkable — not vibes, and not fitted to
one month.
