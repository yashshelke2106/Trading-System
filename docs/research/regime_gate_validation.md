# Regime-Gate Hypothesis — Statistical Validation

Gatekeeper: Statistician desk. Date: 2026-06-25. Branch: phase-h-fast-learn.
Analysis script: `docs/research/_regime_gate_analysis.py` (reproducible, seed 20260625).

## VERDICT: NOT PROVEN (leaning REJECTED)

The hypothesis — that a long-only daily-pullback book loses MORE when NIFTY is
EXTENDED_UP (close>SMA50 AND SMA50>SMA200) than when it is NOT, and that gating
out EXTENDED_UP entries is therefore a real edge — **fails the pre-registered
falsification criteria on multiple independent grounds.** Do NOT ship the gate.

The raw direction matches the hypothesis (NOT bucket has higher PF and mean-R),
and at first glance the effect looks large. But it does not survive the tests
that matter:

1. **Decisive date-clustered permutation p = 0.061 (PF gap) / 0.060 (meanR gap)
   — both >= 0.05.** Pre-registered falsification: clustered p >= 0.05 → FAIL.
2. **Both-halves stability: the effect is strongly positive in the first half
   (+1.24 PF gap) but NEGATIVE in the second, out-of-sample half (-0.12 PF gap,
   -0.14 meanR gap). The gate flips sign OOS.** Pre-registered falsification:
   effect positive in only one half → FAIL.
3. **Concentration / robustness: the NOT bucket's entire positive edge is driven
   by a handful of trades.** NOT bucket sumR = +4.21, but its top-5 R trades sum
   to +9.95 — i.e. without 5 lucky max-R wins the "good" regime is net negative.
   Dropping a single symbol (BEL, 3 trades) collapses the PF gap from +0.65 to
   +0.31. Pre-registered falsification: gap collapses on drop-largest → FAIL.

Any ONE of these falsifies per the stated rules. All three fire. After
multiple-testing correction for the 4 regime definitions the researcher tried,
NO definition clears 0.05 — the primary definition's Bonferroni-corrected p is
0.257, best-of-all is 0.204. Not significant under any correction.

---

## TESTS

### Point-in-time join (no lookahead)
NIFTY ^NSEI daily close fetched via yfinance, 2023-06-01 → 2026-06-25 (756 bars,
0 nulls, range 18,488–26,329 — sane). SMA50/SMA200 computed on the NIFTY series;
SMA200 valid for 100% of the trade window. Each trade labeled with the LAST
NIFTY bar with date <= entry_date. Asserted programmatically that every nifty
bar used is <= its entry_date (max gap 3 days = weekends/holidays). No lookahead.
146 long-only trades, 100 distinct entry-dates, 89 symbols, sumR -17.05.

### Partition (primary EXT_UP)
| Bucket      | n   | mean R  | sum R   | PF    | win% |
|-------------|-----|---------|---------|-------|------|
| EXTENDED_UP | 105 | -0.2025 | -21.26  | 0.591 | 21.9 |
| NOT         | 41  | +0.1027 |  +4.21  | 1.236 | 29.3 |
| **Gap (NOT−EXT)** | | **+0.305** | | **+0.645** | |

Direction matches the hypothesis. Whole book is net-negative (sumR -17.05,
consistent with the prior audit's PF 0.72), so this is "lose less," not "win."

### Test 1 — naive per-trade tests (context only; trades overlap so p is invalid)
- Mann-Whitney U = 2409.5, one-sided p (NOT>EXT) = **0.1188**
- Welch t = 1.433, two-sided p = 0.157, one-sided p = **0.078**

Even ignoring the overlap problem (which inflates significance), neither naive
test clears 0.05. With the overlap correction below, it gets worse.

### Test 2 — DATE-CLUSTERED PERMUTATION (decisive)
Shuffled the EXTENDED_UP label across the **100 distinct entry-dates** (32 NOT-
dates, 68 EXT-dates), holding each date's regime constant across its concurrent
trades, 10,000 reps. This respects the ~9 concurrent / non-independent structure.
- Observed PF gap (NOT−EXT) = **+0.645**; null mean +0.027, null 95% CI
  [-0.639, +0.845]; **clustered one-sided p = 0.0612**
- Observed mean-R gap = **+0.305**; null 95% CI [-0.374, +0.390];
  **clustered one-sided p = 0.0600**

Both above 0.05. The observed gap sits just inside the upper tail of the null —
suggestive, not significant. **This is the decisive test and it does not pass.**

### Test 3 — both-halves stability (split 2025-07-02)
| Half        | nEXT | PF_EXT | nNOT | PF_NOT | PF gap     | meanR gap |
|-------------|------|--------|------|--------|------------|-----------|
| H1 (early)  | 46   | 0.53   | 29   | 1.78   | **+1.241** | +0.522    |
| H2 (late)   | 59   | 0.64   | 12   | 0.52   | **-0.124** | -0.142    |

**The edge exists only in H1 and reverses in H2.** In the later (more out-of-
sample) window the "good" NOT regime actually underperforms the EXTENDED_UP
regime. A gate must improve PF in BOTH halves to be tradeable; it does not. Also
note the NOT bucket is tiny in H2 (n=12) — the late period is mostly EXTENDED_UP.

### Test 4 — robustness / concentration
- Largest-R trade overall (CHOLAFIN, R=1.99) is in the EXT bucket; dropping it
  barely moves the gap (+0.645 → +0.683) — not the driver.
- The driver is in the NOT bucket. NOT sumR = +4.21, but its **top-5 R trades
  sum to +9.95** (MCDOWELL-N, MANAPPURAM, TVSMOTOR, BEL, BEL — all at the R≈1.99
  cap). Strip those 5 and the NOT bucket is net-negative.
- **Dropping one symbol (BEL, 3 trades) collapses the PF gap from +0.645 to
  +0.310 and the mean-R gap from +0.305 to +0.156** — roughly halving the effect
  on the loss of a single name. The edge is not broad; it is a few lucky names in
  one window.

### Test 5 — multiple testing across regime definitions
Same date-clustered permutation (10,000 reps) run on each of the 4 definitions
the researcher explored. Raw clustered p-values (one-sided, gap≥obs):
| Regime def | nNOT / nEXT trades | PF gap | raw clustered p |
|------------|--------------------|--------|-----------------|
| EXT_UP (primary)          | 41 / 105 | +0.645 | 0.0642 |
| close>SMA200              | 11 / 135 | +1.274 | 0.0718 |
| FULLSTACK (SMA200 rising) | 47 / 99  | +0.632 | 0.0509 |
| 20d momentum>0            | 20 / 126 | +0.157 | 0.3730 |

Every directional definition is ABOVE 0.05 even before correction; the closest
(FULLSTACK) is 0.0509. The 20d-momentum cut shows almost no effect (p=0.37),
confirming the signal is specific to the SMA-stack framing, not a robust "trend"
property. (Permutation p's drift ~0.003 run-to-run vs the earlier 0.0612 — same
verdict.)

---

## CORRECTIONS

The researcher tried 4 regime definitions (best-of-N selection inflates
significance). Bonferroni (×4) and Benjamini-Hochberg on the raw clustered p's:
| Regime def | raw p | Bonferroni | BH q |
|------------|-------|------------|------|
| EXT_UP (primary) | 0.0642 | 0.2568 | 0.0957 |
| close>SMA200     | 0.0718 | 0.2872 | 0.0957 |
| FULLSTACK        | 0.0509 | 0.2036 | 0.0957 |
| 20d momentum>0   | 0.3730 | 1.0000 | 0.3730 |

**Trials counted: 4. After correction NONE clear 0.05.** Best Bonferroni = 0.204
(FULLSTACK); best BH q = 0.096. Selection-bias bottom line: a raw p of ~0.06
that is ALREADY above threshold becomes ~0.20–0.29 under Bonferroni. There is no
multiple-testing scenario in which this edge clears significance. (Deflated-Sharpe
logic is identical here: the "best" of 4 correlated regime cuts on the same 146
overlapping trades has a selection-inflated apparent edge; deflating for 4 trials
pushes it well below the bar.)

---

## REGIME (where it holds / breaks)
- HOLDS (descriptively): early window (Sep-2024 → Jul-2025), where NOT-extended
  pullbacks did far better (PF 1.78) than extended-up ones (PF 0.53).
- BREAKS: late window (Jul-2025 → May-2026), where the relationship reverses
  (NOT PF 0.52 < EXT PF 0.64). The late period is dominated by EXTENDED_UP days,
  so the gate would have sat the book out of most of the sample with no benefit.
- The "edge" is also name-concentrated (BEL et al.), not a regime-wide property.

---

## WHY (one paragraph for a PM)
The numbers point the right way — pullback longs did lose less when NIFTY was not
in a confirmed extended uptrend — but the signal is too weak and too fragile to
trade. The decisive overlap-aware test lands at p ≈ 0.06, just outside
significance; the effect is carried almost entirely by the first ~10 months and
reverses in the most recent ~10 months; and a single symbol (BEL) plus five
capped-R winners account for most of the gap. Correct for the four regime
definitions tried and the corrected p is ~0.20–0.26. This is a plausible but
unproven relationship riding on a handful of lucky trades in one regime window —
exactly the kind of "best-of-N in a lucky window" pattern that does not survive
contact with live trading. Do not deploy the regime gate as an edge.

---

## NEXT (if not proven)
1. Do NOT gate on EXTENDED_UP. The whole book is net-negative (PF ~0.72) — the
   real problem is the base strategy, not the regime overlay. Fixing entry/exit
   quality dominates any regime gate worth ~0.06-significance.
2. If the regime idea is pursued, collect FORWARD out-of-sample data: the H1/H2
   flip says the in-sample fit will not generalize. Need >~30 truly independent
   (non-overlapping) NOT-regime trades forward before re-testing.
3. Stop trying multiple regime definitions on the same 146 trades — every extra
   definition raises the correction bar. Pre-register ONE definition next time.
4. Investigate why the relationship reversed in H2 (regime composition? the late
   window is mostly EXTENDED_UP, so the NOT sample is only n=12 and noisy).
