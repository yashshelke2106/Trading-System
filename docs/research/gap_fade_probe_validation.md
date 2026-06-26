# Gap-Fade Probe — Significance Gate (Statistician)

**Sub-thesis under test:** gap-DOWN (`open/prev_close - 1 <= -2%`) -> fade LONG, entered at
the 09:20 5-min bar open, exit at session close.
**Data:** 5-min cache `logs/intraday_5m/` (36 symbols, 58 sessions, 2026-04-01..2026-06-25,
57 usable dates after prev_close). prev_close = prior session's last-bar close.
**Date of gate:** 2026-06-25. Seed-fixed; reproducible via `tmp_stat_test/gap_*.py`.

---

## VERDICT: REJECT — do NOT buy the 3-year intraday data on this thesis.

The edge is **not significant even on this maximally favorable single-regime sample.**
Naive (over-optimistic) tests are already borderline (p ~ 0.055). The moment you account
for the fact that gap events cluster on a handful of macro days — the statistically correct
treatment — the edge dissolves: date-clustered p = 0.16, and the *best* of the 12 cells
that were searched is p = 0.064 (still > 0.05) and dies under any multiple-testing
correction. A power calculation shows the date-level effect is so small (Cohen d = 0.089)
that even the full 3-year buy would not produce enough independent dates to confirm it.
Spending money to acquire data to test a thesis that is statistically invisible at the unit
of independence is not justified.

---

## REPRODUCTION (gate pre-condition — PASSED)

Reconstructed gap-down events directly from the bars:
- n = **50** events (matches probe's n=50)
- raw mean = **+0.5459%** (matches reported +0.546%)
- net @0.06% = **+0.486%**, net @0.12% = **+0.426%**
- **23 distinct dates** for the 50 events (the true independent n is 23, not 50)

Note: the probe brief said "2026-04-08 alone was 22 events." My reconstruction's largest
single-date cluster is **2026-04-13 with 12 events** (04-08 has 1). The exact date/count
differs (likely a different prev_close convention or universe in the original probe), but
the *clustering pathology is identical and the headline number reproduces exactly*, so the
conclusion is unaffected.

---

## TESTS

### Test 1 — Per-event fade returns (gap-down <=-2%, 09:20 -> close, LONG)
| series | n | mean | median | sd | win-rate |
|---|---|---|---|---|---|
| raw | 50 | +0.546% | +0.708% | 2.107 | 0.640 |
| net @0.06% | 50 | +0.486% | +0.648% | 2.107 | 0.620 |
| net @0.12% | 50 | +0.426% | +0.588% | 2.107 | 0.620 |

### Test 2 — Is mean > 0? (NAIVE: treats 50 events as independent = UPPER BOUND of significance)
| test | net@0.06% | net@0.12% |
|---|---|---|
| one-sample t (1-sided >0) | t=1.631, **p=0.0547** | t=1.430, **p=0.0796** |
| sign test (31/50 positive, 1-sided) | **p=0.0595** | p=0.0595 |
| bootstrap 95% CI on mean (naive) | **[-0.103%, +1.054%]** | [-0.165%, +0.991%] |

Even taking every event as independent — which inflates significance — the edge **does not
clear p<0.05**, and the bootstrap CI **straddles zero.** This alone is grounds to reject.

### Test 3 — DATE-CLUSTERED permutation (decisive; 23 distinct dates, 10k reps)
| method | net@0.06% | net@0.12% |
|---|---|---|
| date-mean sign-flip (each date = 1 unit) | obs date-mean +0.153%, **clustered p=0.340** | +0.093%, p=0.398 |
| date-clustered bootstrap (event-weighted) 95% CI | **[-0.479%, +1.214%]** | [-0.537%, +1.148%] |
| date-block sign-flip (event-weighted) | obs +0.486%, **clustered p=0.164** | +0.426%, p=0.192 |

Once events are clustered at the date level — the correct unit of independence — the
clustered p ranges **0.16 to 0.34** and every CI straddles zero. The 50 "trades" are not 50
draws; 23 dates carry the signal and one date (04-13) holds 12 of the 50 events.

---

## CORRECTIONS (multiple testing)

Selection space = {2 thresholds (2%, 3%) x 3 entry times (open, 09:20, 09:30) x 2 directions}
= **12 cells**, best one picked. Clustered p for each cell:

| thr | entry | dir | n | dates | mean_net | clustered p |
|---|---|---|---|---|---|---|
| 3.0 | 09:30 | down_long | 15 | 9 | +1.221% | **0.064** (best) |
| 3.0 | 09:20 | down_long | 15 | 9 | +1.126% | 0.113 |
| 2.0 | 09:20 | down_long | 50 | 23 | +0.486% | **0.166 (the chosen cell)** |
| 2.0 | 09:30 | down_long | 50 | 23 | +0.367% | 0.213 |
| 3.0 | open | down_long | 15 | 9 | +0.691% | 0.219 |
| 2.0 | open | down_long | 50 | 23 | +0.220% | 0.297 |
| (all 6 up-short cells) | | | | | **negative** | 0.67-0.90 |

- **Chosen cell** (2%, 09:20, down_long) clustered p = 0.166.
- **Bonferroni** (alpha=0.05/12=0.0042): corrected p = min(1, 0.166*12) = **1.000**.
- **Benjamini-Hochberg**: not a single cell clears its BH threshold (best cell p=0.064 vs
  BH cutoff 0.0042 at rank 1). **Zero discoveries survive.**
- The "best of N" is the worst offender here: the single most significant cell (3%, 09:30)
  is p=0.064 *before* correction and has only **9 distinct dates / 15 events** — a sample too
  thin to trust.

---

## ROBUSTNESS (net@0.06%, date-block clustered p)

| scenario | n | dates | mean | clustered p |
|---|---|---|---|---|
| BASELINE | 50 | 23 | +0.486% | 0.163 |
| drop largest event (TECHM 2026-06-19, +4.06%) | 49 | 23 | +0.414% | 0.186 |
| drop top symbol by P&L (LT, 3 events, +8.56 sum) | 47 | 22 | +0.335% | 0.237 |
| drop biggest cluster (2026-04-13, 12 events) | 38 | 22 | +0.359% | 0.273 |
| drop 2026-04-08 (per brief) | 49 | 22 | +0.419% | 0.201 |
| EQUAL-WEIGHT per date (one-sample t on 23 date-means) | 23 dates | | +0.153% | t=0.428, **p=0.337**, 10/23 dates positive |

Every robustness perturbation moves the p-value **up**, never toward significance. The
equal-weight-per-date view is the cleanest: **10 of 23 gap-down dates were positive — a coin
flip.** Nothing here survives.

---

## ASYMMETRY RE-CHECK (gap-UP short side)

Gap-UP (>=2%) fade SHORT at 09:20: n=49, 14 dates, mean net = **-0.319%**, win-rate 0.429.
The short side is confirmed negative (consistent with the data-engineer's ~-0.26%). The
long/short asymmetry is real in sign, but that does **not** rescue the long side — the long
side simply isn't significant.

---

## WHAT THIS SAMPLE CANNOT SETTLE

This is **one regime** — a post-crash bull recovery, ~58 trading days, Apr-Jun 2026. Even if
the gap-down fade had cleared significance here, sign-stability across bear / high-vol /
range-bound regimes is **unresolvable** on this window. Only multi-year data could test that.

But there is a harder limit, and it is decisive for the data buy: the **date-level effect
size is Cohen d = 0.089** (date-mean +0.153% / date-sd 1.720 across 23 dates). A power
calculation says detecting an effect this small at 80% power needs **~780 distinct gap-down
dates uncorrected, ~1500 corrected.** A 3-year buy over ~36 symbols would yield on the order
of ~100-150 distinct macro-gap dates — an order of magnitude short. **The effect, at the
magnitude this sample suggests, is statistically undetectable at the unit of independence
even with the full data.**

---

## RECOMMENDATION ON THE DATA BUY: NO-GO for this thesis.

1. **Do not spend money on the 3-year intraday acquisition to validate THIS sub-thesis.**
   It fails the significance gate on the most favorable possible sample (single bull regime,
   best-of-12 cell). The naive p is already 0.055; the honest clustered p is 0.16; corrected
   it is 1.0; and a power calc shows the buy could not confirm it anyway.
2. The gap-down *winrate* (62%) and median (+0.65%) look inviting, but the mean is dragged by
   variance and the events are not independent — classic small-n cluster mirage.
3. If a data buy is justified, justify it on a **different, independently-motivated thesis**,
   not as a rescue for gap-fade. Acquiring data hoping ~50 clustered events become significant
   with more of the same is throwing money at a d=0.089 signal.
4. If anyone insists on keeping gap-fade alive: the only honest path is to treat the **distinct
   gap-down DATE** as the unit and collect to ~150+ such dates, then re-run the date-clustered
   permutation — but go in knowing the power math says it will most likely stay non-significant.

**Bottom line for the PM:** REJECTED. The number that excited the desk (+0.546%, 62% win) is
a 23-date, cluster-driven artifact in one bull regime that does not survive its own
significance test, let alone the best-of-12 selection it was cherry-picked from. Money on the
data buy is not warranted on this thesis.

---

*Artifacts (absolute paths):*
- Reconstruction: `C:\Users\yashs\trading_system\tmp_stat_test\gap_reconstruct.py`
- Tests 1-3: `C:\Users\yashs\trading_system\tmp_stat_test\gap_tests.py`
- Tests 4-5: `C:\Users\yashs\trading_system\tmp_stat_test\gap_robust.py`
- Event tables: `gapdown_events.parquet`, `gap_events_all.parquet` (same dir)
- Honest baseline reference: `C:\Users\yashs\trading_system\core\honest_performance.py`
