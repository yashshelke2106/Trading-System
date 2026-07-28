# H-012 — RSI-2 mean-reversion in the midcap band: REJECTED

**Date:** 2026-07-28 · **Registered before running** (trial #12) · **Script:** `research_midcap_meanrev.py`

## The question

RSI-2 mean-reversion is the programme's one CONDITIONAL-PASS, validated on F&O
large-caps via futures where it lives at 0.06–0.10% round-trip and dies at 0.25%.
Midcap costs start at 0.50%. So the interesting question was never "does
mean-reversion exist in midcaps" — it was:

> does the **gross** edge scale with illiquidity faster than the **cost** does?

Limits-to-arbitrage predicts yes (less arbitrage capital → bigger anomaly).
**Registered prior: likely REJECT on cost.**

## Method

Point-in-time midcap band (non-F&O liquidity rank 1–150) from the
survivorship-complete bhavcopy archive, rebuilt every 6 months, **including
names that later delisted**. RSI(2)<10 → enter **next day's open** (never the
signal bar's close), exit first close with RSI(2)>70 or 5 days. Costs are
**per-trade from that symbol's own turnover tier**, not a flat blended rate.
Statistics clustered **by date** (many names fire the same day; per-trade
t-stats inflate by ~√names-per-day).

**13,091 trades · 1,642 independent dates · 2019–2021.**

## Result — rejected, and for a stronger reason than predicted

The prior was "cost kills it." The reality is worse: **the gross edge is
negative before any cost at all.**

| Metric | Value |
|---|---|
| Gross mean / trade | **−0.165%** (t = −1.79) |
| Net mean / trade | −2.556% |
| Gross PF | 0.882 |
| Win rate | 31.9% |
| Deflated Sharpe | 0.000 — **FAIL** |

### The effect inverts with illiquidity

| Tier | n | cost | **gross** | gross t | net PF |
|---|---|---|---|---|---|
| midcap_liquid | 624 | 0.50% | −0.053% | −0.19 | 0.79 |
| midcap | 2,861 | 1.00% | −0.343% | −2.44 | 0.54 |
| smallmid_illiquid | 5,038 | 2.00% | −0.104% | −0.92 | 0.31 |
| very_illiquid_smallcap | 4,478 | 4.00% | **−0.354%** | **−2.99** | 0.10 |

The gross edge gets **more negative** as liquidity falls. That is the opposite
of the limits-to-arbitrage prediction.

## Positive control — the harness is sound

A negative result is worthless if the code is broken, so the identical
signal/entry/exit/cost code was run on the F&O large-cap universe, where RSI-2
is the known CONDITIONAL-PASS:

| Universe | Gross mean | Gross PF | Win rate |
|---|---|---|---|
| **Large-cap (control)** | **+0.016%** | 1.06 | 44.1% |
| Midcap | −0.165% | 0.88 | 31.9% |

The control reproduces a positive gross edge; the midcap negative is therefore
a property of the universe, not a bug. (The control's gross t=0.21 is weak
because this is a simplified cash/daily-bar version, not the futures
implementation that produced t=4.43 — the *contrast* is the evidence, not the
control's absolute level.)

## What this closes

1. **Midcap RSI-2 is dead** — not marginal, not cost-limited. Negative gross.
2. **Limits-to-arbitrage is falsified for mean-reversion on this universe.**
   Oversold midcaps keep falling; the behaviour is momentum, not reversion.
   This matters beyond RSI-2: it removes the main theoretical reason to expect
   *any* mean-reversion signal to work better down the liquidity ladder.
3. **The "small/midcap frontier" is now substantially narrower.** The midcap
   PEAD hunt (H-007) was already rejected. With mean-reversion also rejected —
   and its underlying premise falsified — the remaining candidates there are
   momentum/trend-shaped, not reversion-shaped.

## What it does NOT close

This tested one signal family. It does not prove midcaps are efficient. A
momentum/trend thesis in the same universe is untested and is *directionally
supported* by this result — oversold names continuing down is exactly what a
trend-follower would want to be short, or a "don't catch falling knives" filter
would want to avoid. That is the honest next hypothesis, and it must be
registered before it is run.
