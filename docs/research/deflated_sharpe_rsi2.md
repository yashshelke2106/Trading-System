# Deflated Sharpe: RSI-2 under selection-bias correction

**Date:** 2026-07-25
**Module:** `core/deflated_sharpe.py` · **Gate:** `core/research_integrity.py` (A5-DSR)

## Why

The program had per-study Bonferroni + one-shot holdout, but no correction for
the fact that a winning result is the **maximum of many trials**. That is the
single most common source of live-vs-backtest disappointment. The Deflated
Sharpe Ratio (Bailey & López de Prado, 2014) is the institutional standard: a
Sharpe must beat not zero, but the Sharpe expected as the best of N trials,
adjusted for sample length and non-normality.

## RSI-2 inputs (from H-009 / docs)

- t = 4.43 on **1,740 independent dates** → per-date Sharpe **0.106** (annualized ≈ 1.69)
- gross PF 1.31; mean-reversion → assume some negative skew + fat tails
- registered program trials = **11** (true count higher — each hunt tried sub-configs)

## Result — the pass is conditional on ONE unmeasured input

DSR survival hinges on the **dispersion of Sharpes across the trials run**:

| trial-Sharpe SD | N=11 | N=30 | N=100 |
|---|---|---|---|
| 0.02 (tight) | PASS 0.999 | PASS 0.996 | PASS 0.990 |
| 0.04 (moderate) | PASS ~0.955 | fail 0.83 | fail 0.58 |
| 0.106 (= winner, conservative) | fail | fail | fail |

Skew/kurtosis moved DSR by <0.01 — **selection bias is the binding constraint,
not non-normality.**

## Verdict

Does **not** overturn RSI-2's existing "CONDITIONAL-PASS, thin" label — it
**quantifies the condition**: RSI-2 is a real edge only if the Sharpes the
program tried were tightly clustered (SD ≤ ~0.03/date). Given the 11 headline
hunts spanned very different theses, moderate dispersion is more likely, which
puts RSI-2 on a knife-edge.

**Concrete next gate before any capital:** compute the actual SD of per-date
Sharpes across every trial run, then re-evaluate. This is now enforced
automatically — `validate_result(..., dsr_T=, dsr_trial_sd=)` raises a hard
A5-DSR flag on failure, and warns when only the conservative default was used.

## What this is / isn't

Risk-reducing only. It can downgrade a claim, never create one. It does not
reopen any rejected hunt; it raises the bar every future pass must clear.


---

## RESOLVED 2026-07-28 — the gate was run, RSI-2 is REJECTED

The open condition above ("compute the actual SD of per-date Sharpes across
every trial run, then re-evaluate") has now been executed. The 2,700-config
sweep in `core/strategy_search.py` over the same universe supplies the
empirical trial-Sharpe distribution that was previously only guessable.

| Quantity | Measured |
|---|---|
| Trial-Sharpe SD | **0.1832** |
| Best trial Sharpe | 0.2492 |
| Unit-free dispersion ratio (sd/best) | **0.735** |
| Ratio RSI-2 needed | **≤ 0.28** |
| Implied `sr_trial_sd` for RSI-2 | 0.0781 (2.6× over the 0.03 threshold) |

Deflated Sharpe with the measured dispersion, at RSI-2's own numbers
(per-date Sharpe 0.1062, T=1740, skew −0.5, kurt 6.0):

| n_trials | SR0 | DSR | verdict |
|---|---|---|---|
| 13 | 0.1330 | 0.140 | **FAIL** |
| 30 | 0.1619 | 0.012 | **FAIL** |
| 100 | 0.1976 | 0.000 | **FAIL** |

**H-009 closed REJECTED.** The gross edge was always real (t=4.43 over 1,740
independent dates); it is simply not separable from what the best of N trials
produces under the null on this universe.

**Caveat, stated plainly:** the dispersion ratio comes from a level-grid trial
family, not from the RSI-2 programme's own more diverse hunts, and the two
Sharpe series are in different units (per-trade vs per-date) — which is why the
unit-free ratio was used rather than a raw substitution. The margin is 2.6×,
wide enough that the conclusion survives that transfer.

With this, the hypothesis registry has **13 trials and zero open questions.**
