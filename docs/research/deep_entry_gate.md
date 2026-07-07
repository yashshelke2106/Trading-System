# Deep-entry refinement — REJECTED (2026-07-07)

**Hypothesis:** restrict the swing screen's long entries to the deep-displacement
trigger only (close 2% under 5-DMA), dropping the shallow RSI-2 trigger. The
15-year trade log's cause analysis showed +57bp vs +5bp per trade in favor of
deep entries.

**Test:** each trigger run as a STANDALONE strategy (not a trade-log split),
100 stocks, 2011–2026, long-only, risk-on gate, 2/2 ATR, hold≤10, cash 0.25%.
Gates: date-clustered t, time halves, cross-sectional stock split (odd/even),
and a paired daily comparison.

| Variant | n | PF | avg | clust t | stocks-A t | stocks-B t |
|---|---|---|---|---|---|---|
| CURRENT any-of-3 | 14,135 | 1.09 | +18.0bp | **+2.18** | +1.45 | +1.53 |
| DEEP only | 6,778 | 1.19 | +41.6bp | +1.44 | **+0.41** | +1.98 |
| SHALLOW only | 13,743 | 1.07 | +14.8bp | +2.32 | +1.60 | +1.49 |
| **PAIRED deep−mix** | 1,784 days | — | **−0.1bp/day** | **t=−0.01** | — | — |

**Verdict: REJECT — keep the any-of-3 mix.**
1. The paired test is decisive: on common trading days the deep filter adds
   exactly nothing (t=−0.01).
2. Deep-only's higher per-trade average is offset by half the trade count; its
   clustered significance is LOWER (1.44 vs 2.18) and it is unstable across
   the stock split (t=+0.41 in one half — the "edge" lives in a random subset).
3. The original +57bp/+5bp split was a **priority-ordering artifact**: in the
   trade log, the deep trigger fired first whenever both were true, so the
   RSI-2-only bucket was, by construction, the mild-displacement leftovers.
   Classic selection illusion — looked like signal quality, was bookkeeping.
4. Note: H2 (2019–2026) t≈+0.4–0.9 everywhere — the long edge itself is
   fading in the modern era. Consistent with every prior finding.

**Standing rule reaffirmed:** in-sample splits of a trade log are hypotheses,
not findings. Any screen change must pass this gate first. Signal weighting is
delegated to the live SwingLearner (Wilson-gated, ≥20 resolved trades/bucket).
