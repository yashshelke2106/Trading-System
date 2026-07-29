# The exact edge required — found, quantified, and located

**Date:** 2026-07-28 · **Script:** `research_edge_required.py` · **Evidence:** 2,559 survivorship-complete symbols, up to 265k sampled entries per width

---

## The answer, in one line

```
edge_required  =  cost / (target + stop)
```

All in the same % units. **Only the sum of the levels matters.** Cost is a fixed
toll; widening the bracket spreads it thinner, so the edge you must supply falls.

Derivation — a bracket is profitable when `hit > (1 + c/S)/(1 + T/S)`, and the
driftless random-walk null is `p₀ = S/(S+T)`. Subtract: `edge_req = c/(T+S)`.
Verified numerically: expectancy at exactly this edge = 0.000000R.

### Specialised to a 7-in-10 hit rate

Reaching 70% requires the null itself to sit at 70%, i.e. `S ≥ 2.333·T`.
Substituting:

```
edge_required  =  cost / (3.333 × target)
```

At 0.20% round-trip cost:

| Target | Stop | Edge required |
|---|---|---|
| 1.5% | 3.5% | **4.00%** |
| 3.0% | 7.0% | **2.00%** |
| 6.0% | 14.0% | **1.00%** |
| 9.0% | 21.0% | **0.67%** |
| 12.0% | 28.0% | **0.50%** |

**That is the exact answer to "what edge do we need."**

---

## Does the market supply it?

Measured on the survivorship-complete archive (2,559 symbols, delisted names
included), intrabar ties blended at the measured P(target-first) = 0.904:

| Target | Hold | n | Hit | Edge | Required | Margin | Verdict |
|---|---|---|---|---|---|---|---|
| 1.5% | 20d | 264,989 | 66.9% | −3.12% | 4.00% | −7.12% | fail |
| 3.0% | 40d | 261,205 | 67.9% | −2.15% | 2.00% | −4.15% | fail |
| 6.0% | 90d | 251,847 | 70.3% | +0.25% | 1.00% | −0.75% | fail |
| 9.0% | 150d | 240,514 | 72.8% | **+2.81%** | 0.67% | **+2.14%** | **passes** |
| 12.0% | 250d | 221,208 | 76.1% | **+6.05%** | 0.50% | **+5.55%** | **passes** |

So in aggregate the edge **does** exist — but only at 150–250 day holds.

### Survivorship mattered, a lot

The first run used today's `FO_UNIVERSE` applied to 2019 history — survivorship
bias, using 4.3% of names selected by *today's* status. Re-running
survivorship-complete **killed the 6%/90d configuration**:

| | FO_UNIVERSE (biased) | Survivorship-complete |
|---|---|---|
| 6% / 90d edge | +3.17% → passes | **+0.25% → fails** |

One config was pure survivorship artefact. This is why the honest arm is the default.

---

## What the edge actually is — and why it is not a strategy

The test window ran at **+16.7% CAGR** (equal-weight archive proxy, 2019→2026).
Drift over a 250-day hold is **+16.6%** — which *exceeds* the 12% target. The
"edge" is the equity risk premium being harvested by a wide bracket. It is
**beta, not alpha.**

And it is not stable. Splitting by entry-year cohort:

**target 9% / stop 21% / hold 150d**

| Entry year | Hit | Edge | Verdict |
|---|---|---|---|
| 2019 | 67.1% | −2.93% | fail |
| 2020 | 80.0% | +9.99% | ok |
| 2021 | 80.5% | +10.55% | ok |
| 2022 | 68.0% | −1.96% | fail |
| 2023 | 85.3% | +15.25% | ok |
| 2024 | 69.4% | −0.56% | fail |
| **2025** | **61.1%** | **−8.87%** | **fail** |

**3 of 7 cohorts work.** The aggregate is carried entirely by 2020, 2021 and
2023 — the post-COVID rally. The two most recent years both fail.

The 12%/250d config is only marginally better (4 of 7), and **2024 and 2025 both
fail there too.**

---

## The verdict

**The exact edge needed is `cost / (target + stop)` — between 0.50% and 4.00%
depending on bracket width.**

**Where it exists:** only in the equity risk premium, only at 150–250 day holds,
and only in bull cohorts. In 4 of 7 years it is absent, including both of the
most recent.

That is not an edge you can trade. It is *exposure*, and it arrives with a stop
loss attached that will whipsaw you out in exactly the years it fails.

### Which points at the thing you already have

If the only reliable edge is the equity risk premium harvested over 7–12 month
horizons, the correct instrument is not a bracketed stock trade. It is a
diversified index holding — no stop to be whipsawed by, no single-name risk, no
per-trade cost drag:

> **the allocation engine (index-core + 200-DMA overlay)**

The search for an edge converges on the position this programme reached by a
completely different route in June. The 7-in-10 framing, followed rigorously all
the way down, lands on buy-the-index.

**Search concluded.** The edge required is known exactly. The only source that
supplies it is beta, and the vehicle for beta is already built.
