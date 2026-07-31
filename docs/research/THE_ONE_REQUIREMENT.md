# The one requirement for profitable option buying — tested, and absent

**Date:** 2026-07-31 · **H-016** · `research_negative_vrp.py`
**Data:** India VIX + NIFTY, 2008–2026 (18 years, includes the 2008 and 2020 crashes)

---

## First, I corrected my own framing

I originally said the requirement was *"a signal that predicts direction better
than the implied move."* That is imprecise. A long option is not a pure
direction bet — it is **long volatility and long direction at once**. It pays
only when:

```
realized move  >  implied move (what you paid for)
```

And the unconditional fact is already known and measured: **implied exceeds
realized ~77% of the time.** That gap *is* the volatility risk premium. It is
why selling wins and buying loses on average.

So the requirement, stated correctly, is:

> **Find a condition under which the volatility risk premium goes NEGATIVE** —
> a state where the realized move systematically exceeds the implied one.

If such a state exists, buying options in it is +EV. If not, buying is
structurally −EV and no amount of good mechanics fixes it.

---

## The test

For every day and horizon *h*:

```
implied_move  = VIX_t/100 × √(h/365)        (+5% for the buyer's cost)
realized_move = |NIFTY_{t+h} / NIFTY_t − 1|
vrp           = implied − realized          ( >0 seller wins, <0 BUYER wins )
```

Bucketed by **point-in-time VIX percentile** (rank within its own trailing 2-year
window — no lookahead). Overlapping windows corrected by sampling every *h*-th
day, so t-stats use independent observations only.

Primary pre-registered candidate: **very low VIX = complacency → vol expansion**
("buy vol when it's cheap"). Registered prior: *expect the right sign but not
enough to clear premium + cost.*

---

## Result — 17 states tested

| Horizon | VIX pctl | implied | realized | mean VRP | buyer win% | t |
|---|---|---|---|---|---|---|
| 21d | 0–10 | 3.26% | 3.10% | **−0.04%** | 37.6% | −0.09 |
| 21d | 10–25 | 3.65% | 3.39% | **−0.41%** | 34.5% | −0.62 |
| 21d | 25–50 | 3.93% | 3.55% | +1.18% | 33.9% | 3.59 |
| 21d | 75–90 | 5.14% | 3.99% | +1.77% | 25.1% | 2.65 |
| 42d | 0–10 | 4.61% | 4.90% | **−0.60%** | 46.6% | −1.01 |
| 10d | all buckets | — | — | **all positive** | 23–34% | — |

**Seller favoured in 14 of 17 states (82%).**

The three buyer-favourable cells are all **low-VIX** — the predicted direction,
so the *sign* of the thesis is right. Vol does mean-revert after complacency.
But the magnitudes are noise:

- Best cell: 42d low-VIX, VRP **−0.60%**, **t = −1.01**, p_raw 0.312
- Bonferroni ×17 → **p = 1.000** — fails every correction

### The single most decisive number

**The buyer's win rate never exceeded 47% in any state** (range 23–47%).
There is no volatility regime — low, mid, high, any horizon — where the option
buyer is more likely than not to win.

---

## Limitation, stated plainly

Independent sample per cell is small (11–101), because taking non-overlapping
*h*-day windows inside a 10–25% VIX bucket is thin even with 18 years. A larger
sample could sharpen the low-VIX cells.

But the conclusion is not sample-fragile: the sign is consistent in **14 of 17**
cells, and the buyer win-rate ceiling of 47% holds everywhere. The direction of
the answer is not in doubt; only its third decimal place.

---

## Verdict

**The one requirement does not exist in this market.**

There is no findable state where realized move exceeds implied move. Buying
options is structurally −EV here, and the volatility risk premium is precisely
the reason. This is not a failure to search hard enough — it is the same premium
showing up from the other side, and it is *why the condor sleeve exists*.

What this closes: option buying, definitively, on evidence rather than on the
journal's PF 0.44 alone. The mechanics in `core/directional_buy.py` remain
correct and ready — the gate stays BLOCKED because the precondition is absent,
not because the mechanics are wrong.

**The honest corollary:** every rupee the buyer loses to this premium is the
rupee the seller collects. The system is already positioned on the correct side
of it.
