# The strategy-search agent: built, run, and what it found

**Date:** 2026-07-28 · **Module:** `core/strategy_search.py` · **Registered as:** H-013

---

## What was asked for

An agent that continuously modifies and tries different strategies until it
finds one where 7 of 10 trades hit target.

## What was built

Exactly that — with the one guard that makes it worth having.

An unguarded "keep trying until something works" agent is a **false-positive
machine**. Search 2,700 configurations against one dataset and you will find
several with 70%+ hit rates and positive backtest expectancy *by luck alone*.
The harder such an agent tries, the more confidently wrong it becomes. So this
one is built around the correction rather than bolting it on:

| Guard | What it does |
|---|---|
| Trial counting | Every config evaluated increments N; the winner is judged against the best-of-N null, not against zero |
| Locked holdout | Final 30% of dates split off **before** the search; only finalists touch it, once |
| Deflated Sharpe | Survivor's Sharpe must beat what the best of N random trials would produce |
| Null benchmark | Every config scored against `S/(S+T)`, since level placement moves hit rate without creating profit |
| Honest reporting | If nothing survives, it says so. It is **not permitted** to return a "best available" config as if validated |

**Search space:** 15 entry filters × 3 market regimes × 5 targets × 4 stops ×
3 holds = **2,700 configurations**, over 273,229 symbol-days from the
survivorship-complete archive.

### Intrabar resolution — measured, not assumed

When a daily bar contains both target and stop, their order is unknown.
Assuming target-first inflates results; assuming stop-first deflates them.
Both are computed per row and blended with **P(target first) = 0.904**, which
was *measured* on real 5-minute bars (261 ambiguous bars, 151 symbols). That
window is 60 days and possibly bull-skewed, so it is treated as an
optimistic-leaning estimate.

---

## What it found

```
configurations evaluated        : 2700
  reaching 70% hit rate         : 814
  profitable                    : 55
  BOTH                          : 0
  survived locked holdout       : 0
  survived selection deflation  : 0
```

**814 configurations reach 7-in-10. 55 are profitable. The overlap is empty.**

That is the win-rate/payoff trade-off made concrete across 2,700 attempts.

### The best candidate, and why it still fails

```
uptrend / mkt_down · target 1.0% · stop 5.0% · hold 10d · n = 13,979
hit rate 84.3%   null 83.3%   edge +1.01%   expectancy −0.028R
```

It has a genuinely **positive** edge over the random-walk null. It still loses,
because costs are larger than the edge:

| | |
|---|---|
| Gross expectancy | +0.0121R |
| Cost at 0.20% round-trip | 0.0400R |
| **Break-even round-trip cost** | **6.04 bps** |

Best-case NSE futures cost is ~6.0 bps. The strategy sits *exactly* on the
boundary — meaning it is not viable with any realistic slippage, and 0.06% is a
best case rather than an average.

### The decisive test: is that +1.01% edge even real?

It is the best of 2,700 trials, so the honest comparison is against what a
2,700-config search produces **on pure noise**. Simulating the null
(hit rate ~ Binomial(n, `S/(S+T)`), 2,000 repetitions):

| | Edge |
|---|---|
| Observed best over 2,700 trials | **+1.01%** |
| Noise best-of-2,700, mean | **+1.09%** |
| Noise best-of-2,700, 95th pct | +1.28% |
| **P(noise ≥ observed)** | **0.81** |

**The winner is weaker than the typical noise winner.** And because the 2,700
configs share data and overlap heavily, the effective number of independent
trials is below 2,700 — which makes this null *conservative*, i.e. generous to
the strategy.

The agent searched exhaustively and found nothing. That is the correct result,
and the agent reporting it as nothing is the feature.

---

## A real bug this work caught

Writing the noise test exposed a defect in `core/deflated_sharpe.py`: the
default trial-dispersion stand-in was `sr_hat`, which can be **negative**. A
standard deviation cannot be negative, and a negative value flipped the sign of
the `SR0` benchmark — so a *losing* strategy would "beat" it.

Measured before the fix: **Sharpe −0.05 scored DSR 0.999 and PASSED.** A
money-loser certified as validated — the worst possible failure for a gate.

Fixed by taking `abs()` of the dispersion, rejecting negative inputs in
`expected_max_sharpe`, and adding an explicit `sr_hat > 0` condition to
`passes`. Regression-tested. This bug existed in the gate used by earlier
conclusions; none of them relied on a negative Sharpe, so no prior verdict
changes — but it would have mattered the first time something genuinely lost.

---

## Bottom line

The agent exists, it works, and you can re-run it any time as the journal and
archive grow:

```bash
python -m core.strategy_search --search --target-wr 0.70 --cost 0.20
```

What it will keep telling you, until something in the market or the cost
structure changes:

> 7-in-10 is easy. 7-in-10 *and profitable* is not available in this universe at
> retail costs. The gap is ~6 basis points wide and the edge that would have to
> fill it is indistinguishable from noise.

The agent's job is not to find a winner. It is to stop you funding a loser that
looks like one — and across 2,700 attempts, that is exactly what it did.
