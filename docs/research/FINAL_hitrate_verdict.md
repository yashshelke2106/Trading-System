# FINAL: can this system deliver 7-in-10 target hits?

**Date:** 2026-07-28 · **Script:** `research_hitrate_frontier.py` · **Evidence:** 53,288 sampled entries per configuration, 153 F&O large-caps, survivorship-complete archive 2019–2026

---

## The one-line answer

**Yes, easily — and it loses money every time.**

You can have 88% target hits. You cannot have 88% target hits *and* a profit.
These are not the same request, and the difference is not a matter of effort or
tuning. It is arithmetic.

---

## Why "7 out of 10" is the wrong target to optimise

Hit rate is set by where you put the levels, not by skill. Move the target
closer and the stop wider, and the hit rate rises to any number you like:

| Target | Stop | Hit rate | Net expectancy |
|---|---|---|---|
| 0.5% | 5.0% | **88.0%** | **−0.072R** |
| 0.5% | 3.0% | **79.4%** | −0.140R |
| 1.0% | 5.0% | **80.2%** | −0.078R |
| 1.0% | 3.0% | **71.1%** | −0.119R |
| 1.5% | 5.0% | **73.1%** | −0.090R |

Five configurations clear 7-in-10. **All five lose money.** You win small, often;
you lose big, occasionally; the occasional loss is larger than the frequent wins
combined.

---

## The theorem that governs it

For a driftless random walk, the probability of touching +T before −S is

```
P_null = S / (S + T)
```

and the break-even win rate for that same pair is

```
WR_breakeven = 1 / (1 + T/S) = S / (S + T)
```

**These are identical.** A coin-flip market yields exactly zero expectancy at
*every* reward:risk ratio. Choosing levels cannot manufacture profit — it only
moves you along a curve where win rate and payoff trade off exactly.

Therefore the **only** source of profit is the real hit rate *exceeding* `P_null`.
That surplus is "edge." Costs are then subtracted from it.

---

## Measured edge: essentially zero, and negative after costs

| Target/Stop | Actual hit | Random-walk null | **Edge** |
|---|---|---|---|
| 0.5% / 3.0% | 79.4% | 85.7% | **−6.3%** |
| 1.0% / 3.0% | 71.1% | 75.0% | **−3.9%** |
| 1.0% / 5.0% | 80.2% | 83.3% | **−3.2%** |
| 1.5% / 5.0% | 73.1% | 76.9% | **−3.9%** |

Mean edge across the whole grid: **−5.69%**. Not one of 20 cells is positive.

### The result survives the strongest challenge to it

Daily bars cannot tell you whether the target or the stop was touched first when
both sit inside one day's range. Resolving those to "target" would fake a large
edge — the classic mirage. The main run resolves every ambiguous bar to **stop**
(maximally pessimistic). To prove the finding is not merely that assumption,
the identical study was re-run with the **maximally optimistic** rule (target
always wins ties — physically impossible, since it assumes you were always lucky):

| Target/Stop | Edge (pessimistic) | Edge (optimistic) | Reaches 70%? |
|---|---|---|---|
| 0.5% / 3.0% | −6.3% | **−0.9%** | yes |
| 1.0% / 3.0% | −3.9% | **−0.5%** | yes |
| 1.0% / 5.0% | −3.2% | **−2.2%** | yes |
| 1.5% / 5.0% | −3.9% | **−3.1%** | yes |
| 1.0% / 2.0% | −6.6% | +0.7% | no (67%) |
| 2.0% / 2.0% | −2.4% | +0.9% | no (51%) |

The truth lies between the two columns. **Every configuration that reaches 70%
is negative under both bounds.** The two cells that turn positive under the
impossible-luck assumption do not reach 70% in the first place.

This is as close to a closed question as measurement gets: at these horizons the
large-cap tape is a random walk to within ±1%, and costs are larger than that.

---

## What the live system actually produces

| Metric | Value |
|---|---|
| Journal win rate | **33.6%** |
| Profit factor | **0.44** |
| Expectancy | **−0.54% / trade** |
| Clean trades | 1,012 |

The gap between 33.6% and 70% is not a tuning gap. The system's targets are wide
relative to its stops; that *choice* is why the hit rate is low. Narrowing them
would raise the hit rate toward 70% and leave expectancy just as negative — as
the table above demonstrates directly.

---

## What would genuinely produce 7-in-10 profitably

Only a real edge, i.e. a hit rate above `P_null`. Everything this programme has
tested for one is a closed negative:

| Hunt | Verdict |
|---|---|
| Intraday gap-fade | REJECTED |
| PEAD (large-cap) | REJECTED |
| PEAD (midcap) | REJECTED |
| Vol-managed overlay | REJECTED |
| Index-rebalance flow | REJECTED |
| Corporate-event drift (13 types) | REJECTED |
| Regime gate | REJECTED |
| Cash-equity delivery premise | REJECTED |
| Midcap mean-reversion (H-012) | REJECTED — *and premise falsified* |
| RSI-2 via futures | CONDITIONAL-PASS — thin, cost-fragile, not live |

Twelve registered trials. One survivor, and it is conditional.

---

## The verdict

**The goal as stated is achievable and worthless; the goal as intended is not
achievable on this universe with this stack.**

What *is* achievable, and is already built:

- **The allocation engine** (index-core + 200-DMA overlay) — earns the market
  return cheaply. Its "hit rate" is irrelevant because it is not trying to win
  individual trades; it captures the equity premium. This is the validated
  money path.
- **The learning loop** (mistake guards + keeper boosts + recurrence retirement)
  — raises the *quality* of whatever is traded, and honestly reports when it
  cannot prove itself.
- **The research stack** — registry, holdout, Bonferroni, Deflated Sharpe,
  survivorship-complete data. This is what lets a claim be killed in a day
  instead of costing a year of capital.

The honest summary: this system is now **excellent at telling you the truth**
about trading ideas, including uncomfortable ones. It is not, and on this
evidence cannot be, a 7-in-10 profitable signal generator. Those two facts are
related — the same rigour that refuses to fake a 70% win rate is what makes
every other number it reports trustworthy.
